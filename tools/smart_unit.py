"""Unit checks for dw_smart: smartctl JSON (ATA, NVMe, SCSI) and the Windows WMI attribute
block are turned into the same report and the same verdict rules.  Runs anywhere:
    python tools/smart_unit.py
"""
import json
import os
import sys

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, here)
import dw_smart  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


DISK = {"id": "disk:1", "name": "Disk 1", "number": 1, "path": "\\\\.\\PhysicalDrive1", "model": "x", "serial": "y", "size": 1000204886016, "logicalSector": 512, "bus": "SATA"}


def ata_json(reallocated=0, pending=0, passed=True, temp=34, value5=100):
    def attr(i, name, value, worst, thresh, raw, prefail=True, when=""):
        return {"id": i, "name": name, "value": value, "worst": worst, "thresh": thresh, "when_failed": when,
                "flags": {"value": 51 if prefail else 50, "prefailure": prefail, "updated_online": True},
                "raw": {"value": raw, "string": str(raw)}}
    return {
        "json_format_version": [1, 0], "smartctl": {"version": [7, 5], "exit_status": 0, "messages": []},
        "device": {"name": "/dev/sda", "type": "sat", "protocol": "ATA"},
        "model_family": "Samsung based SSDs", "model_name": "Samsung SSD 870 EVO 1TB", "serial_number": "S6PTNS0R123456X", "firmware_version": "SVT02B6Q",
        "user_capacity": {"blocks": 1953525168, "bytes": 1000204886016}, "logical_block_size": 512, "rotation_rate": 0,
        "form_factor": {"name": "2.5 inches"}, "sata_version": {"string": "SATA 3.3"},
        "smart_support": {"available": True, "enabled": True}, "smart_status": {"passed": passed},
        "ata_smart_data": {"self_test": {"status": {"value": 0, "string": "completed without error", "passed": True}, "polling_minutes": {"short": 2, "extended": 85}},
                           "capabilities": {"self_tests_supported": True}},
        "ata_smart_attributes": {"revision": 1, "table": [
            attr(5, "Reallocated_Sector_Ct", value5, value5, 10, reallocated),
            attr(9, "Power_On_Hours", 99, 99, 0, 4321, False),
            attr(12, "Power_Cycle_Count", 99, 99, 0, 876, False),
            attr(177, "Wear_Leveling_Count", 97, 97, 0, 45),
            attr(190, "Airflow_Temperature_Cel", 66, 50, 0, temp, False),
            attr(197, "Current_Pending_Sector", 100, 100, 0, pending, False),
            attr(198, "Offline_Uncorrectable", 100, 100, 0, 0, False),
            attr(199, "UDMA_CRC_Error_Count", 100, 100, 0, 3, False),
            attr(241, "Total_LBAs_Written", 99, 99, 0, 56_000_000_000, False),
        ]},
        "ata_smart_self_test_log": {"standard": {"revision": 1, "table": [
            {"type": {"value": 1, "string": "Short offline"}, "status": {"value": 0, "string": "Completed without error", "passed": True}, "lifetime_hours": 4300}], "count": 1}},
        "power_on_time": {"hours": 4321}, "power_cycle_count": 876, "temperature": {"current": temp},
    }


def nvme_json(crit=0, used=3, spare=100, thresh=10, media=0):
    return {
        "json_format_version": [1, 0], "smartctl": {"version": [7, 5], "exit_status": 0},
        "device": {"name": "/dev/nvme0", "type": "nvme", "protocol": "NVMe"},
        "model_name": "WD_BLACK SN850X 2000GB", "serial_number": "24010A800123", "firmware_version": "620361WD",
        "nvme_total_capacity": 2000398934016, "nvme_version": {"string": "1.4"}, "logical_block_size": 512,
        "smart_support": {"available": True, "enabled": True}, "smart_status": {"passed": crit == 0, "nvme": {"value": crit}},
        "nvme_smart_health_information_log": {"critical_warning": crit, "temperature": 41, "available_spare": spare, "available_spare_threshold": thresh,
                                              "percentage_used": used, "data_units_read": 12_345_678, "data_units_written": 23_456_789, "host_reads": 1, "host_writes": 2,
                                              "controller_busy_time": 345, "power_cycles": 120, "power_on_hours": 2200, "unsafe_shutdowns": 7, "media_errors": media,
                                              "num_err_log_entries": 0, "warning_temp_time": 0, "critical_comp_time": 0},
        "nvme_self_test_log": {"current_self_test_operation": {"value": 0, "string": "No self-test in progress"}, "table": [
            {"self_test_code": {"value": 1, "string": "Short"}, "self_test_result": {"value": 0, "string": "Completed without error"}, "power_on_hours": 2100}]},
        "temperature": {"current": 41}, "power_cycle_count": 120, "power_on_time": {"hours": 2200},
    }


def win_block(reallocated=0, pending=0, temp=35):
    data = [16, 0] + [0] * 510
    th = [16, 0] + [0] * 510
    entries = [(5, 0x33, 100, 100, reallocated, 10), (9, 0x32, 98, 98, 11000, 0), (12, 0x32, 99, 99, 500, 0),
               (194, 0x22, 35, 20, temp | (45 << 16), 0), (197, 0x32, 100, 100, pending, 0), (198, 0x30, 100, 100, 0, 0), (241, 0x32, 99, 99, 123456789, 0)]
    for i, (aid, flags, value, worst, raw, thresh) in enumerate(entries):
        o = 2 + 12 * i
        data[o] = aid
        data[o + 1] = flags & 0xFF
        data[o + 2] = flags >> 8
        data[o + 3] = value
        data[o + 4] = worst
        for k in range(6):
            data[o + 5 + k] = (raw >> (8 * k)) & 0xFF
        th[o] = aid
        th[o + 1] = thresh
    return data, th


print("ATA (healthy)")
r = dw_smart.normalize_smartctl(DISK, ata_json(), "smartctl 7.5")
check(r["verdict"] == "good" and not r["reasons"], f"verdict good: {r['verdict']} {r['reasons']}")
check(r["kind"] == "ata" and r["model"] == "Samsung SSD 870 EVO 1TB" and r["rotation"] == 0, "identity from the json")
check(r["temperature"] == 34 and r["powerOnHours"] == 4321 and r["powerCycles"] == 876, f"summary fields: {r['temperature']} {r['powerOnHours']} {r['powerCycles']}")
check(r["hostWritten"] == 56_000_000_000 * 512, f"Total_LBAs_Written * 512: {r['hostWritten']}")
check(r["lifeUsed"] == 3, f"life used from Wear_Leveling_Count value 97: {r['lifeUsed']}")
check(len(r["attributes"]) == 9 and all(a["flag"] in ("ok", "info") for a in r["attributes"]), "all attributes ok (CRC errors are info only)")
check(r["selfTest"]["shortMinutes"] == 2 and r["selfTest"]["log"][0]["passed"] is True and r["selfTest"]["supported"], "self-test data")

print("ATA (reallocated + pending)")
r = dw_smart.normalize_smartctl(DISK, ata_json(reallocated=8, pending=2), "smartctl 7.5")
check(r["verdict"] == "caution" and len(r["reasons"]) == 2, f"caution with two reasons: {r['verdict']} {r['reasons']}")
check(next(a for a in r["attributes"] if a["id"] == 5)["flag"] == "warn", "attribute 5 flagged warn")

print("ATA (attribute below threshold / status failed)")
r = dw_smart.normalize_smartctl(DISK, ata_json(reallocated=900, value5=5), "smartctl 7.5")
check(r["verdict"] == "bad" and "threshold" in r["reasons"][0], f"bad: {r['reasons'][:1]}")
r = dw_smart.normalize_smartctl(DISK, ata_json(passed=False), "smartctl 7.5")
check(r["verdict"] == "bad" and "FAILED" in r["reasons"][0], "SMART status failed -> bad")
r = dw_smart.normalize_smartctl(DISK, ata_json(temp=63), "smartctl 7.5")
check(r["verdict"] == "caution" and "hot" in r["reasons"][0], f"63 C -> caution: {r['reasons']}")

print("NVMe")
r = dw_smart.normalize_smartctl(DISK, nvme_json(), "smartctl 7.5")
check(r["verdict"] == "good" and r["kind"] == "nvme", f"healthy nvme: {r['verdict']} {r['reasons']}")
check(r["hostWritten"] == 23_456_789 * 512000 and r["hostRead"] == 12_345_678 * 512000, "data units * 512000")
check(r["lifeUsed"] == 3 and r["temperature"] == 41 and r["powerOnHours"] == 2200, "nvme summary fields")
check(r["selfTest"]["log"][0]["type"] == "Short" and r["selfTest"]["supported"], "nvme self-test log")
r = dw_smart.normalize_smartctl(DISK, nvme_json(crit=4), "smartctl 7.5")
check(r["verdict"] == "bad" and any("reliability degraded" in x for x in r["reasons"]), f"critical warning 0x04 -> bad: {r['reasons']}")
r = dw_smart.normalize_smartctl(DISK, nvme_json(spare=5, thresh=10), "smartctl 7.5")
check(r["verdict"] == "bad" and "spare" in r["reasons"][0].lower(), "spare below threshold -> bad")
r = dw_smart.normalize_smartctl(DISK, nvme_json(used=95, media=3), "smartctl 7.5")
check(r["verdict"] == "caution" and len(r["reasons"]) == 2, f"95 % used + media errors -> caution x2: {r['reasons']}")

print("Windows WMI attribute block")
data, th = win_block()
attrs = dw_smart.parse_ata_block(data, th)
check(len(attrs) == 7 and attrs[0]["id"] == 5 and attrs[0]["thresh"] == 10 and attrs[0]["prefail"], f"7 attributes parsed, thresholds joined: {attrs[0]}")
check(next(a for a in attrs if a["id"] == 241)["raw"] == 123456789, "48-bit raw value")
rep = dw_smart.blank_report(DISK)
rep["attributes"] = attrs
rep["counters"] = {"healthStatus": "Healthy", "predictFailure": False}
dw_smart._derive_from_attributes(rep, 512)
dw_smart.assess(rep)
check(rep["temperature"] == 35 and rep["powerOnHours"] == 11000 and rep["powerCycles"] == 500, f"derived: {rep['temperature']} {rep['powerOnHours']} {rep['powerCycles']}")
check(rep["hostWritten"] == 123456789 * 512, "written from attribute 241")
check(rep["verdict"] == "good", f"windows block healthy: {rep['verdict']} {rep['reasons']}")
data, th = win_block(pending=5)
rep = dw_smart.blank_report(DISK)
rep["attributes"] = dw_smart.parse_ata_block(data, th)
rep["counters"] = {"healthStatus": "Warning", "predictFailure": True, "predictReason": 3}
dw_smart.assess(rep)
check(rep["verdict"] == "bad" and "predicts its own failure" in rep["reasons"][0], f"PredictFailure -> bad: {rep['reasons']}")

print("instance matching")
pnp = r"SCSI\Disk&Ven_Samsung&Prod_SSD_870_EVO\4&2a0b5c8a&0&000000"
m = dw_smart._match_instance([{"InstanceName": pnp + "_0", "PredictFailure": False}, {"InstanceName": r"IDE\Other_0"}], pnp.upper())
check(m is not None and m["InstanceName"].startswith("SCSI"), "InstanceName = PNPDeviceID + _0 (case-insensitive)")

print("empty / unreadable")
rep = dw_smart.normalize_smartctl(DISK, {"smartctl": {"messages": [{"string": "Unable to detect device type", "severity": "error"}], "exit_status": 1}}, "smartctl 7.5")
check(rep["verdict"] == "unknown" and rep["warnings"] == ["Unable to detect device type"], f"unknown with the message: {rep['verdict']} {rep['warnings']}")

print("device names")
check(dw_smart.device_for({"number": 2, "path": "\\\\.\\PhysicalDrive2"}) == ("/dev/pd2" if os.name == "nt" else "\\\\.\\PhysicalDrive2"), "windows -> /dev/pdN")

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} check(s) failed")
    sys.exit(1)
print("RESULT: all checks passed")
