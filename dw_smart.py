"""Drive health (S.M.A.R.T.): read a disk's self-monitoring data through the elevated helper
and turn it into one plain verdict plus the details (ARCHITECTURE.md §6, decision D-037).

Sources, in order:
  1. smartctl (smartmontools): bundled on Windows (bin/win64/smartmontools/smartctl.exe) and
     Linux (bin/linux-x86_64/tools/smartctl), Homebrew on macOS.  `smartctl -j -a <device>`,
     retried with `-d sat` for USB bridges.  ATA attributes, the NVMe health log, SCSI
     counters, the self-test log and status.
  2. Windows without smartctl: Get-PhysicalDisk (HealthStatus) + Get-StorageReliabilityCounter
     (temperature, power-on hours, wear, read / write errors) + the WMI failure-prediction
     classes (MSStorageDriver_FailurePredict{Status,Data,Thresholds}) which carry the raw ATA
     attribute block for drives the ATA pass-through driver exposes.
  3. macOS without smartctl: `diskutil info -plist` SMARTStatus (Verified / Failing / Not Supported).

The verdict follows what CrystalDiskInfo shows: **Bad** when the drive itself says so (SMART
status failed, an attribute at or below its threshold, an NVMe critical warning, spare below
its threshold); **Caution** for reallocated / pending / uncorrectable sectors, NVMe
percentage-used >= 90 or media errors, 60 °C or more; **Good** otherwise; **Unknown** when
nothing could be read.  The helper side runs the tools (root / administrator is needed for
every one of them); the window side caches one report per disk and serves the routes.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0
CACHE_SECONDS = 120
TEMP_CAUTION = 60          # °C, CrystalDiskInfo's default alarm
NVME_USED_CAUTION = 90     # percentage used
DATA_UNIT = 512_000        # NVMe data units are 1000 × 512 bytes

# ATA attribute names for the Windows fallback (smartctl supplies its own)
ATA_NAMES = {
    1: "Raw_Read_Error_Rate", 2: "Throughput_Performance", 3: "Spin_Up_Time", 4: "Start_Stop_Count", 5: "Reallocated_Sector_Ct",
    7: "Seek_Error_Rate", 8: "Seek_Time_Performance", 9: "Power_On_Hours", 10: "Spin_Retry_Count", 11: "Calibration_Retry_Count",
    12: "Power_Cycle_Count", 13: "Read_Soft_Error_Rate", 160: "Uncorrectable_Error_Cnt", 161: "Valid_Spare_Blocks", 163: "Initial_Bad_Blocks",
    164: "Total_Erase_Count", 165: "Max_Erase_Count", 166: "Min_Erase_Count", 167: "Average_Erase_Count", 168: "Max_Erase_Count_of_Spec",
    169: "Remaining_Lifetime_Perc", 170: "Available_Reservd_Space", 171: "Program_Fail_Count", 172: "Erase_Fail_Count", 173: "Wear_Leveling_Count",
    174: "Unexpect_Power_Loss_Ct", 175: "Program_Fail_Count_Chip", 176: "Erase_Fail_Count_Chip", 177: "Wear_Leveling_Count", 178: "Used_Rsvd_Blk_Cnt_Chip",
    179: "Used_Rsvd_Blk_Cnt_Tot", 180: "Unused_Rsvd_Blk_Cnt_Tot", 181: "Program_Fail_Cnt_Total", 182: "Erase_Fail_Count_Total", 183: "Runtime_Bad_Block",
    184: "End-to-End_Error", 187: "Reported_Uncorrect", 188: "Command_Timeout", 189: "High_Fly_Writes", 190: "Airflow_Temperature_Cel",
    191: "G-Sense_Error_Rate", 192: "Power-Off_Retract_Count", 193: "Load_Cycle_Count", 194: "Temperature_Celsius", 195: "Hardware_ECC_Recovered",
    196: "Reallocated_Event_Count", 197: "Current_Pending_Sector", 198: "Offline_Uncorrectable", 199: "UDMA_CRC_Error_Count", 200: "Multi_Zone_Error_Rate",
    201: "Soft_Read_Error_Rate", 202: "Percent_Lifetime_Remain", 206: "Flying_Height", 225: "Host_Writes_32MiB", 230: "Life_Curve_Status",
    231: "SSD_Life_Left", 232: "Available_Reservd_Space", 233: "Media_Wearout_Indicator", 235: "POR_Recovery_Count", 240: "Head_Flying_Hours",
    241: "Total_LBAs_Written", 242: "Total_LBAs_Read", 246: "Total_LBAs_Written", 247: "Host_Program_Page_Count", 248: "FTL_Program_Page_Count",
    249: "NAND_Writes_1GiB", 250: "Read_Error_Retry_Rate", 254: "Free_Fall_Sensor",
}
# raw value > 0 means the drive has had to work around bad media (CrystalDiskInfo's caution set)
CAUTION_IDS = {5: ("reallocated sector", "reallocated sectors"), 196: ("reallocation event", "reallocation events"),
               197: ("sector waiting to be reallocated", "sectors waiting to be reallocated"), 198: ("uncorrectable sector", "uncorrectable sectors")}
# normalised value = life remaining on these SSD attributes
LIFE_IDS = (231, 233, 202, 169, 177, 173)
TEMP_IDS = (194, 190)


# ----------------------------------------------------------------------------
# Tool location
# ----------------------------------------------------------------------------
def resource_dir() -> str:
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


def smartctl_path() -> str | None:
    """The bundled smartctl when present, else one installed on the system."""
    cands = []
    if IS_WIN:
        cands.append(os.path.join(resource_dir(), "bin", "win64", "smartmontools", "smartctl.exe"))
    elif IS_LINUX:
        cands.append(os.path.join(resource_dir(), "bin", "linux-x86_64", "tools", "smartctl"))
    for c in cands:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    found = shutil.which("smartctl")
    if found:
        return found
    for c in ("/opt/homebrew/sbin/smartctl", "/opt/homebrew/bin/smartctl", "/usr/local/sbin/smartctl", "/usr/local/bin/smartctl", "/usr/sbin/smartctl"):
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def smartctl_version(exe: str) -> str:
    try:
        r = subprocess.run([exe, "-j", "-V"], capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW, env=_env())
        j = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
        v = j.get("smartctl", {}).get("version")
        if v:
            return "smartctl " + ".".join(str(x) for x in v)
    except Exception:
        pass
    return "smartctl"


def _env() -> dict:
    env = dict(os.environ, LC_ALL="C", LANG="C")
    if IS_LINUX:
        try:
            import dw_linux
            return dw_linux.tool_env()
        except ImportError:
            pass
    return env


def device_for(disk: dict) -> str:
    """What smartctl calls this disk: /dev/pdN on Windows, the device path elsewhere."""
    if IS_WIN:
        return f"/dev/pd{disk.get('number')}"
    return str(disk.get("path") or "")


def run_smartctl(exe: str, argv: list[str], log=None, title: str = "", timeout: float = 120.0) -> tuple[int, dict, str]:
    cmd = [exe, "-j"] + argv
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW, env=_env())
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    try:
        data = json.loads(out) if out.strip() else {}
    except ValueError:
        data = {}
    if log:
        log(" ".join(["smartctl"] + cmd[1:]), r.returncode, (time.time() - t0) * 1000, (out[-6000:] + ("\n" + err if err.strip() else "")).strip(), title)
    return r.returncode, data, err


# ----------------------------------------------------------------------------
# Normalising a smartctl JSON report
# ----------------------------------------------------------------------------
def _get(d: dict, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def blank_report(disk: dict) -> dict:
    return {"disk": disk.get("id"), "diskName": disk.get("name"), "device": None, "source": None, "ts": time.time(),
            "verdict": "unknown", "reasons": [], "warnings": [],
            "model": disk.get("model"), "serial": disk.get("serial"), "firmware": None, "capacity": disk.get("size"),
            "kind": None, "rotation": None, "formFactor": None, "interface": None,
            "smartSupported": None, "smartEnabled": None, "passed": None,
            "temperature": None, "tempMax": None, "powerOnHours": None, "powerCycles": None,
            "hostWritten": None, "hostRead": None, "lifeUsed": None,
            "attributes": [], "nvme": None, "scsi": None, "selfTest": None, "counters": None}


def normalize_smartctl(disk: dict, j: dict, exe_label: str) -> dict:
    rep = blank_report(disk)
    rep["source"] = exe_label
    rep["device"] = _get(j, "device", "name")
    rep["warnings"] = [m.get("string", "") for m in _get(j, "smartctl", "messages", default=[]) if m.get("severity") in ("error", "warning")]
    rep["model"] = j.get("model_name") or rep["model"]
    rep["family"] = j.get("model_family")
    rep["serial"] = j.get("serial_number") or rep["serial"]
    rep["firmware"] = j.get("firmware_version")
    rep["capacity"] = _get(j, "user_capacity", "bytes") or j.get("nvme_total_capacity") or rep["capacity"]
    proto = (_get(j, "device", "protocol") or "").lower()
    rep["kind"] = "nvme" if "nvme" in proto or "nvme_smart_health_information_log" in j else "scsi" if proto == "scsi" or "scsi_vendor" in j else "ata" if proto or "ata_smart_attributes" in j else None
    rep["rotation"] = j.get("rotation_rate")
    rep["formFactor"] = _get(j, "form_factor", "name")
    rep["interface"] = _get(j, "sata_version", "string") or _get(j, "interface_speed", "current", "string") or (_get(j, "nvme_version", "string") and "NVMe " + _get(j, "nvme_version", "string"))
    rep["smartSupported"] = _get(j, "smart_support", "available")
    rep["smartEnabled"] = _get(j, "smart_support", "enabled")
    rep["passed"] = _get(j, "smart_status", "passed")
    rep["temperature"] = _get(j, "temperature", "current")
    rep["powerOnHours"] = _get(j, "power_on_time", "hours")
    rep["powerCycles"] = j.get("power_cycle_count")

    # ATA attributes
    sector = int(j.get("logical_block_size") or disk.get("logicalSector") or 512)
    for a in _get(j, "ata_smart_attributes", "table", default=[]) or []:
        try:
            att = {"id": int(a.get("id")), "name": a.get("name") or ATA_NAMES.get(int(a.get("id")), ""), "value": a.get("value"), "worst": a.get("worst"),
                   "thresh": a.get("thresh"), "raw": _get(a, "raw", "value"), "rawString": _get(a, "raw", "string"),
                   "prefail": bool(_get(a, "flags", "prefailure")), "whenFailed": a.get("when_failed") or ""}
        except (TypeError, ValueError):
            continue
        rep["attributes"].append(att)
    _derive_from_attributes(rep, sector)

    # NVMe health log
    nv = j.get("nvme_smart_health_information_log")
    if isinstance(nv, dict):
        rep["nvme"] = {
            "criticalWarning": nv.get("critical_warning"), "temperature": nv.get("temperature"),
            "availableSpare": nv.get("available_spare"), "spareThreshold": nv.get("available_spare_threshold"),
            "percentageUsed": nv.get("percentage_used"),
            "dataRead": (nv.get("data_units_read") or 0) * DATA_UNIT if nv.get("data_units_read") is not None else None,
            "dataWritten": (nv.get("data_units_written") or 0) * DATA_UNIT if nv.get("data_units_written") is not None else None,
            "hostReads": nv.get("host_reads"), "hostWrites": nv.get("host_writes"),
            "controllerBusyMinutes": nv.get("controller_busy_time"), "powerCycles": nv.get("power_cycles"),
            "powerOnHours": nv.get("power_on_hours"), "unsafeShutdowns": nv.get("unsafe_shutdowns"),
            "mediaErrors": nv.get("media_errors"), "errorLogEntries": nv.get("num_err_log_entries"),
            "warningTempMinutes": nv.get("warning_temp_time"), "criticalTempMinutes": nv.get("critical_comp_time"),
        }
        if rep["temperature"] is None:
            rep["temperature"] = nv.get("temperature")
        if rep["powerOnHours"] is None:
            rep["powerOnHours"] = nv.get("power_on_hours")
        if rep["powerCycles"] is None:
            rep["powerCycles"] = nv.get("power_cycles")
        rep["hostWritten"] = rep["nvme"]["dataWritten"]
        rep["hostRead"] = rep["nvme"]["dataRead"]
        if nv.get("percentage_used") is not None:
            rep["lifeUsed"] = nv.get("percentage_used")

    # SCSI / SAS counters
    if rep["kind"] == "scsi":
        rep["scsi"] = {"grownDefects": j.get("scsi_grown_defect_list"), "errorCounters": j.get("scsi_error_counter_log"),
                       "startStop": j.get("scsi_start_stop_cycle_counter"), "percentageUsed": _get(j, "scsi_percentage_used_endurance_indicator")}
        if rep["scsi"]["percentageUsed"] is not None:
            rep["lifeUsed"] = rep["scsi"]["percentageUsed"]
        if rep["temperature"] is None:
            rep["temperature"] = _get(j, "temperature", "current")

    # self-tests
    st = _get(j, "ata_smart_data", "self_test")
    nst = j.get("nvme_self_test_log")
    if isinstance(st, dict) or isinstance(nst, dict):
        sel = {"status": _get(st, "status", "string") if isinstance(st, dict) else None,
               "passed": _get(st, "status", "passed") if isinstance(st, dict) else None,
               "remaining": _get(st, "status", "remaining_percent") if isinstance(st, dict) else None,
               "shortMinutes": _get(st, "polling_minutes", "short") if isinstance(st, dict) else None,
               "longMinutes": _get(st, "polling_minutes", "extended") if isinstance(st, dict) else None,
               "supported": bool(_get(j, "ata_smart_data", "capabilities", "self_tests_supported", default=isinstance(nst, dict))),
               "log": []}
        if isinstance(nst, dict):
            cur = nst.get("current_self_test_operation") or {}
            if cur.get("value"):
                sel["status"] = cur.get("string") or "Self-test in progress"
                sel["remaining"] = 100 - int(nst.get("current_self_test_completion_percent") or 0)
            for e in nst.get("table") or []:
                sel["log"].append({"type": _get(e, "self_test_code", "string"), "status": _get(e, "self_test_result", "string"),
                                   "passed": _get(e, "self_test_result", "value") == 0, "hours": e.get("power_on_hours"), "lba": e.get("lba")})
        for e in _get(j, "ata_smart_self_test_log", "standard", "table", default=[]) or []:
            sel["log"].append({"type": _get(e, "type", "string"), "status": _get(e, "status", "string"), "passed": _get(e, "status", "passed"),
                               "hours": e.get("lifetime_hours"), "lba": e.get("lba")})
        rep["selfTest"] = sel

    assess(rep)
    return rep


def _derive_from_attributes(rep: dict, sector: int) -> None:
    """Temperature, hours, cycles, host writes and life from the ATA table when the summary fields are missing."""
    by_id = {a["id"]: a for a in rep["attributes"]}
    if rep["temperature"] is None:
        for tid in TEMP_IDS:
            a = by_id.get(tid)
            if a and a.get("raw") is not None:
                t = int(a["raw"]) & 0xFF            # many drives pack min/max into the upper bytes
                if 0 < t < 120:
                    rep["temperature"] = t
                    break
    if rep["powerOnHours"] is None and 9 in by_id and by_id[9].get("raw") is not None:
        rep["powerOnHours"] = int(by_id[9]["raw"]) & 0xFFFFFFFF
    if rep["powerCycles"] is None and 12 in by_id and by_id[12].get("raw") is not None:
        rep["powerCycles"] = int(by_id[12]["raw"])
    if rep["hostWritten"] is None:
        for wid in (241, 246, 225, 249):
            a = by_id.get(wid)
            if a and a.get("raw"):
                name = (a.get("name") or "").lower()
                unit = 32 * 2**20 if "32mib" in name or wid == 225 else 2**30 if "gib" in name or wid == 249 else sector
                rep["hostWritten"] = int(a["raw"]) * unit
                break
    if rep["hostRead"] is None and 242 in by_id and by_id[242].get("raw"):
        name = (by_id[242].get("name") or "").lower()
        rep["hostRead"] = int(by_id[242]["raw"]) * (32 * 2**20 if "32mib" in name else sector)
    if rep["lifeUsed"] is None:
        for lid in LIFE_IDS:
            a = by_id.get(lid)
            if a and isinstance(a.get("value"), int) and 0 <= a["value"] <= 100:
                rep["lifeUsed"] = 100 - a["value"]
                break


# ----------------------------------------------------------------------------
# Verdict
# ----------------------------------------------------------------------------
def assess(rep: dict) -> None:
    bad, caution = [], []
    if rep.get("passed") is False:
        bad.append("The drive reports its own S.M.A.R.T. status as FAILED.")
    for a in rep.get("attributes", []):
        v, t = a.get("value"), a.get("thresh")
        a["flag"] = "ok"
        if isinstance(v, int) and isinstance(t, int) and t > 0 and v <= t:
            a["flag"] = "bad"
            bad.append(f"{a['name'] or a['id']} is at or below its threshold ({v} ≤ {t}).")
        elif a["id"] in CAUTION_IDS and isinstance(a.get("raw"), int) and a["raw"] > 0:
            a["flag"] = "warn"
            caution.append(f"{a['raw']:,} {CAUTION_IDS[a['id']][0 if a['raw'] == 1 else 1]} ({a['name'] or a['id']}).")
        elif a["id"] == 199 and isinstance(a.get("raw"), int) and a["raw"] > 0:
            a["flag"] = "info"
    nv = rep.get("nvme")
    if nv:
        cw = nv.get("criticalWarning")
        if isinstance(cw, int) and cw:
            flags = []
            if cw & 1:
                flags.append("spare capacity below threshold")
            if cw & 2:
                flags.append("temperature outside limits")
            if cw & 4:
                flags.append("NVM subsystem reliability degraded")
            if cw & 8:
                flags.append("media set read-only")
            if cw & 16:
                flags.append("volatile memory backup failed")
            bad.append("NVMe critical warning: " + (", ".join(flags) or f"0x{cw:02x}") + ".")
        if nv.get("availableSpare") is not None and nv.get("spareThreshold") is not None and nv["availableSpare"] < nv["spareThreshold"]:
            bad.append(f"Available spare {nv['availableSpare']} % is below the drive's threshold of {nv['spareThreshold']} %.")
        if isinstance(nv.get("percentageUsed"), int) and nv["percentageUsed"] >= NVME_USED_CAUTION:
            caution.append(f"{nv['percentageUsed']} % of the rated endurance has been used.")
        if isinstance(nv.get("mediaErrors"), int) and nv["mediaErrors"] > 0:
            caution.append(f"{nv['mediaErrors']:,} media and data-integrity errors logged.")
    sc = rep.get("scsi")
    if sc and isinstance(sc.get("grownDefects"), int) and sc["grownDefects"] > 0:
        caution.append(f"{sc['grownDefects']:,} grown defects.")
    if isinstance(rep.get("lifeUsed"), int) and rep["lifeUsed"] >= NVME_USED_CAUTION and not nv:
        caution.append(f"{rep['lifeUsed']} % of the rated SSD life has been used.")
    t = rep.get("temperature")
    if isinstance(t, (int, float)) and t >= TEMP_CAUTION:
        caution.append(f"{t:.0f} °C is hot for a drive (alarm at {TEMP_CAUTION} °C).")
    cnt = rep.get("counters")
    if cnt:
        if cnt.get("predictFailure"):
            bad.append("Windows reports the drive predicts its own failure" + (f" (reason {cnt['predictReason']})" if cnt.get("predictReason") else "") + ".")
        if cnt.get("healthStatus") and str(cnt["healthStatus"]).lower() not in ("healthy", "0", "ok"):
            caution.append(f"Windows health status: {cnt['healthStatus']}.")
        if isinstance(cnt.get("readErrorsUncorrected"), int) and cnt["readErrorsUncorrected"] > 0:
            caution.append(f"{cnt['readErrorsUncorrected']:,} uncorrected read errors.")
        if isinstance(cnt.get("writeErrorsUncorrected"), int) and cnt["writeErrorsUncorrected"] > 0:
            caution.append(f"{cnt['writeErrorsUncorrected']:,} uncorrected write errors.")
        if isinstance(cnt.get("wear"), int) and cnt["wear"] >= NVME_USED_CAUTION:
            caution.append(f"Windows reports {cnt['wear']} % wear.")
    readable = rep.get("passed") is not None or rep.get("attributes") or nv or sc or (cnt and any(v is not None for v in cnt.values()))
    if bad:
        rep["verdict"] = "bad"
    elif caution:
        rep["verdict"] = "caution"
    elif readable:
        rep["verdict"] = "good"
    else:
        rep["verdict"] = "unknown"
    rep["reasons"] = bad + caution


# ----------------------------------------------------------------------------
# Windows fallback: storage reliability counters + WMI failure prediction
# ----------------------------------------------------------------------------
WIN_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$n = [int]$env:DW_DISK
$out = @{}
$pd = Get-PhysicalDisk | Where-Object { [string]$_.DeviceId -eq [string]$n } | Select-Object -First 1
if ($pd) {
  $out.pd = @{ FriendlyName = $pd.FriendlyName; SerialNumber = $pd.SerialNumber; FirmwareVersion = $pd.FirmwareVersion; MediaType = [string]$pd.MediaType;
               BusType = [string]$pd.BusType; HealthStatus = [string]$pd.HealthStatus; OperationalStatus = [string]($pd.OperationalStatus -join ', '); Size = $pd.Size }
  $rc = $pd | Get-StorageReliabilityCounter
  if ($rc) { $out.rc = @{ Temperature = $rc.Temperature; TemperatureMax = $rc.TemperatureMax; PowerOnHours = $rc.PowerOnHours; StartStopCycleCount = $rc.StartStopCycleCount;
                          ReadErrorsTotal = $rc.ReadErrorsTotal; ReadErrorsCorrected = $rc.ReadErrorsCorrected; ReadErrorsUncorrected = $rc.ReadErrorsUncorrected;
                          WriteErrorsTotal = $rc.WriteErrorsTotal; WriteErrorsCorrected = $rc.WriteErrorsCorrected; WriteErrorsUncorrected = $rc.WriteErrorsUncorrected;
                          Wear = $rc.Wear; LoadUnloadCycleCount = $rc.LoadUnloadCycleCount; ManufactureDate = [string]$rc.ManufactureDate } }
}
$dd = Get-CimInstance Win32_DiskDrive | Where-Object { $_.Index -eq $n } | Select-Object -First 1
if ($dd) { $out.dd = @{ PNPDeviceID = $dd.PNPDeviceID; Model = $dd.Model; SerialNumber = $dd.SerialNumber; FirmwareRevision = $dd.FirmwareRevision; Size = $dd.Size; InterfaceType = $dd.InterfaceType } }
$out.status = @(Get-CimInstance -Namespace root\wmi -ClassName MSStorageDriver_FailurePredictStatus | ForEach-Object { @{ InstanceName = $_.InstanceName; PredictFailure = [bool]$_.PredictFailure; Reason = $_.Reason } })
$out.data = @(Get-CimInstance -Namespace root\wmi -ClassName MSStorageDriver_FailurePredictData | ForEach-Object { @{ InstanceName = $_.InstanceName; VendorSpecific = [int[]]$_.VendorSpecific } })
$out.thresholds = @(Get-CimInstance -Namespace root\wmi -ClassName MSStorageDriver_FailurePredictThresholds | ForEach-Object { @{ InstanceName = $_.InstanceName; VendorSpecific = [int[]]$_.VendorSpecific } })
$out | ConvertTo-Json -Depth 5 -Compress
"""


def _match_instance(entries: list, pnp: str | None) -> dict | None:
    if not pnp:
        return None
    key = pnp.lower()
    for e in entries or []:
        name = str(e.get("InstanceName") or "").lower()      # "<PNPDeviceID>_0"
        if name.startswith(key) or key.startswith(name.rsplit("_", 1)[0]):
            return e
    return None


def parse_ata_block(data: list[int] | None, thresholds: list[int] | None) -> list[dict]:
    """The 512-byte SMART READ DATA / READ THRESHOLDS blocks as Windows hands them out."""
    out = []
    if not data or len(data) < 2 + 12:
        return out
    th = {}
    if thresholds and len(thresholds) >= 2 + 12:
        for i in range(30):
            o = 2 + 12 * i
            if o + 12 > len(thresholds):
                break
            if thresholds[o]:
                th[thresholds[o]] = thresholds[o + 1]
    for i in range(30):
        o = 2 + 12 * i
        if o + 12 > len(data):
            break
        aid = data[o]
        if not aid:
            continue
        flags = data[o + 1] | (data[o + 2] << 8)
        raw = 0
        for k in range(6):
            raw |= (data[o + 5 + k] & 0xFF) << (8 * k)
        out.append({"id": aid, "name": ATA_NAMES.get(aid, f"Attribute_{aid}"), "value": data[o + 3], "worst": data[o + 4],
                    "thresh": th.get(aid), "raw": raw, "rawString": str(raw), "prefail": bool(flags & 1), "whenFailed": ""})
    return out


def windows_fallback(disk: dict, log=None) -> dict:
    import dw_win
    rep = blank_report(disk)
    rep["source"] = "Windows storage counters"
    rep["device"] = disk.get("path")
    code, out, err = dw_win.run_powershell(WIN_SCRIPT.replace("$env:DW_DISK", str(int(disk.get("number") or 0))), timeout=120, log=log, title="Read drive health (Windows)")
    try:
        j = json.loads(out.strip() or "{}")
    except ValueError:
        raise RuntimeError("Windows did not return drive health data" + (f": {err.strip()[-200:]}" if err.strip() else "."))
    pd, rc, dd = j.get("pd") or {}, j.get("rc") or {}, j.get("dd") or {}
    rep["model"] = pd.get("FriendlyName") or dd.get("Model") or rep["model"]
    rep["serial"] = (pd.get("SerialNumber") or dd.get("SerialNumber") or rep["serial"] or "").strip()
    rep["firmware"] = pd.get("FirmwareVersion") or dd.get("FirmwareRevision")
    rep["capacity"] = pd.get("Size") or dd.get("Size") or rep["capacity"]
    bus = (pd.get("BusType") or "").lower()
    rep["kind"] = "nvme" if "nvme" in bus else "scsi" if bus in ("sas", "scsi", "iscsi") else "ata" if bus in ("sata", "ata", "usb") else None
    rep["interface"] = pd.get("BusType")
    rep["rotation"] = 0 if (pd.get("MediaType") or "").upper() == "SSD" else None
    rep["counters"] = {
        "healthStatus": pd.get("HealthStatus"), "operationalStatus": pd.get("OperationalStatus"),
        "temperature": rc.get("Temperature"), "temperatureMax": rc.get("TemperatureMax"), "powerOnHours": rc.get("PowerOnHours"),
        "startStopCycles": rc.get("StartStopCycleCount"), "readErrorsTotal": rc.get("ReadErrorsTotal"), "readErrorsCorrected": rc.get("ReadErrorsCorrected"),
        "readErrorsUncorrected": rc.get("ReadErrorsUncorrected"), "writeErrorsTotal": rc.get("WriteErrorsTotal"), "writeErrorsCorrected": rc.get("WriteErrorsCorrected"),
        "writeErrorsUncorrected": rc.get("WriteErrorsUncorrected"), "wear": rc.get("Wear"), "loadUnloadCycles": rc.get("LoadUnloadCycleCount"),
        "manufactureDate": rc.get("ManufactureDate") or None, "predictFailure": None, "predictReason": None,
    }
    if rc.get("Temperature"):
        rep["temperature"] = rc.get("Temperature")
        rep["tempMax"] = rc.get("TemperatureMax") or None
    rep["powerOnHours"] = rc.get("PowerOnHours") or None
    rep["powerCycles"] = rc.get("StartStopCycleCount") or None
    if isinstance(rc.get("Wear"), int):
        rep["lifeUsed"] = rc.get("Wear")
    pnp = dd.get("PNPDeviceID")
    st = _match_instance(j.get("status") or [], pnp)
    if st:
        rep["counters"]["predictFailure"] = bool(st.get("PredictFailure"))
        rep["counters"]["predictReason"] = st.get("Reason")
        rep["passed"] = not st.get("PredictFailure")
    da = _match_instance(j.get("data") or [], pnp)
    th = _match_instance(j.get("thresholds") or [], pnp)
    if da:
        rep["attributes"] = parse_ata_block(da.get("VendorSpecific"), (th or {}).get("VendorSpecific"))
        rep["source"] = "Windows storage counters + ATA attributes (WMI)"
        _derive_from_attributes(rep, int(disk.get("logicalSector") or 512))
    if rep["counters"]["healthStatus"] is None and not da and not rc:
        rep["warnings"].append("Windows exposes no health data for this disk (USB enclosures and virtual disks usually do not).")
    assess(rep)
    return rep


# ----------------------------------------------------------------------------
# macOS fallback
# ----------------------------------------------------------------------------
def mac_fallback(disk: dict, log=None) -> dict:
    import dw_mac
    rep = blank_report(disk)
    rep["source"] = "diskutil"
    rep["device"] = disk.get("path")
    info = dw_mac.plist(["/usr/sbin/diskutil", "info", "-plist", str(disk.get("path"))])
    status = str(info.get("SMARTStatus") or "")
    rep["model"] = info.get("MediaName") or rep["model"]
    rep["rotation"] = 0 if info.get("SolidState") else None
    if status.lower() == "verified":
        rep["passed"] = True
    elif status.lower() in ("failing", "failed"):
        rep["passed"] = False
    else:
        rep["warnings"].append("macOS cannot read S.M.A.R.T. from this drive (external drives over USB need `brew install smartmontools`).")
    assess(rep)
    return rep


# ----------------------------------------------------------------------------
# Helper verbs (run as root / administrator)
# ----------------------------------------------------------------------------
def read_health(helper, rid: int, args: dict) -> dict:
    disk = helper.find_disk(str(args.get("disk")))
    log = helper.log_cmd(rid)
    exe = smartctl_path()
    rep = None
    if exe:
        dev = device_for(disk)
        label = smartctl_version(exe)
        attempts = [[]]
        if (disk.get("bus") or "").upper() == "USB" or IS_MAC:
            attempts += [["-d", "sat"], ["-d", "sat,12"], ["-d", "scsi"]]
        last_msgs = []
        for extra in attempts:
            code, j, err = run_smartctl(exe, ["-a"] + extra + [dev], log=log, title="Read drive health")
            opened = bool(j.get("model_name") or j.get("serial_number") or j.get("ata_smart_attributes") or j.get("nvme_smart_health_information_log") or j.get("smart_status"))
            if opened:
                rep = normalize_smartctl(disk, j, label + (" (" + " ".join(extra) + ")" if extra else ""))
                break
            last_msgs = [m.get("string", "") for m in _get(j, "smartctl", "messages", default=[])] or ([err.strip()] if err.strip() else [])
        if rep is None:
            rep = blank_report(disk)
            rep["source"] = label
            rep["device"] = dev
            rep["warnings"] = [m for m in last_msgs if m][:3] or ["smartctl could not open this device."]
    # platform fallbacks when smartctl is missing or could not read the drive
    if rep is None or rep["verdict"] == "unknown":
        try:
            if IS_WIN:
                alt = windows_fallback(disk, log=log)
            elif IS_MAC:
                alt = mac_fallback(disk, log=log)
            else:
                alt = None
            if alt is not None and (rep is None or alt["verdict"] != "unknown"):
                alt["warnings"] = (rep["warnings"] if rep else []) + alt["warnings"]
                rep = alt
        except Exception as e:
            if rep is None:
                rep = blank_report(disk)
            rep["warnings"].append(str(e))
    if rep is None:
        rep = blank_report(disk)
        rep["warnings"].append("No S.M.A.R.T. reader is available on this system" + (" — install smartmontools (`apt install smartmontools` / `brew install smartmontools`)." if not IS_WIN else "."))
    rep["smartctl"] = bool(exe)
    return rep


def start_selftest(helper, rid: int, args: dict) -> dict:
    disk = helper.find_disk(str(args.get("disk")))
    kind = "long" if str(args.get("kind")) == "long" else "short"
    exe = smartctl_path()
    if not exe:
        raise RuntimeError("Self-tests need smartctl (smartmontools), which is not available on this system.")
    log = helper.log_cmd(rid)
    code, j, err = run_smartctl(exe, ["-t", kind, device_for(disk)], log=log, title=f"Start {kind} self-test")
    msgs = [m.get("string", "") for m in _get(j, "smartctl", "messages", default=[])]
    if code & 0x03:
        raise RuntimeError("smartctl could not start the test: " + ("; ".join(m for m in msgs if m) or err.strip() or f"exit {code}"))
    minutes = _get(j, "ata_smart_data", "self_test", "polling_minutes", kind if kind == "short" else "extended")
    return {"ok": True, "kind": kind, "minutes": minutes, "messages": msgs}


def helper_verbs(helper) -> dict:
    return {"smart": lambda rid, args: read_health(helper, rid, args),
            "smart_selftest": lambda rid, args: start_selftest(helper, rid, args)}


# ----------------------------------------------------------------------------
# Window side: routes and the per-disk cache
# ----------------------------------------------------------------------------
class Smart:
    def __init__(self, jobs):
        self.jobs = jobs
        self.app = jobs.app
        self.cache: dict[str, dict] = {}

    def handle_get(self, h, path: str, q: dict) -> bool:
        if path == "/api/smart":
            disk = q.get("disk", [""])[0]
            refresh = q.get("refresh", ["0"])[0] == "1"
            h._json(self.report(disk, refresh))
            return True
        if path == "/api/smart/all":
            h._json({"reports": {k: self._summary(v) for k, v in self.cache.items()}})
            return True
        return False

    def handle_post(self, h, path: str, body: dict) -> bool:
        if path == "/api/smart/selftest":
            disk = str(body.get("disk") or "")
            r = self.jobs.helper().request("smart_selftest", {"disk": disk, "kind": body.get("kind") or "short"}, timeout=120, on_event=self.jobs._forward_log)
            self.cache.pop(disk, None)
            self.app.info(f"S.M.A.R.T. {r.get('kind')} self-test started on {disk}")
            h._json(r)
            return True
        return False

    def report(self, disk: str, refresh: bool = False) -> dict:
        if not disk:
            raise RuntimeError("Which disk?")
        if not self.app.helper_ready():
            return {"locked": True, "disk": disk, "message": "Reading S.M.A.R.T. data needs administrator rights. Press Unlock first."}
        hit = self.cache.get(disk)
        if hit and not refresh and time.time() - hit.get("ts", 0) < CACHE_SECONDS:
            return hit
        rep = self.jobs.helper().request("smart", {"disk": disk}, timeout=300, on_event=self.jobs._forward_log)
        rep["ts"] = time.time()
        self.cache[disk] = rep
        self.app.info(f"S.M.A.R.T. read on {rep.get('diskName') or disk}: {rep.get('verdict')}" + (f" — {rep['reasons'][0]}" if rep.get("reasons") else ""))
        return rep

    @staticmethod
    def _summary(rep: dict) -> dict:
        return {"verdict": rep.get("verdict"), "temperature": rep.get("temperature"), "powerOnHours": rep.get("powerOnHours"), "ts": rep.get("ts"),
                "reasons": rep.get("reasons", [])[:2]}
