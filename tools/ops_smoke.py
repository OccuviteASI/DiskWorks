"""End-to-end test of the operations engine against a real (test) disk.

    python tools/ops_smoke.py <port> '<ops json>' [--plan-only]

Starts DiskWorks headless, unlocks (set DISKWORKS_NO_ELEVATE=1 to skip UAC/pkexec when
already root/admin), plans the given operations, prints the steps, applies them
(unless --plan-only), streams the job events, then prints the touched disks.
Operation ids may use the placeholders DISK=<name>, GAP0=<disk name> (first gap of that
disk) and PART=<disk name>:<number> which are resolved against the live inventory.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

port = int(sys.argv[1])
ops = json.loads(sys.argv[2])
plan_only = "--plan-only" in sys.argv
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATTACH = "--attach" in sys.argv   # drive an already-running instance (e.g. the dev server) instead of starting one
proc = None if ATTACH else subprocess.Popen([sys.executable, os.path.join(here, "diskworks.py"), "--no-open", "--port", str(port)],
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
base = f"http://127.0.0.1:{port}"


def get(path):
    with urllib.request.urlopen(base + path, timeout=60) as r:
        return json.loads(r.read().decode())


def post(path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def resolve(inv, value):
    if not isinstance(value, str):
        return value
    m = re.match(r"^DISK=(.+)$", value)
    if m:
        return next(d["id"] for d in inv["disks"] if d["name"] == m.group(1))
    m = re.match(r"^GAP0=(.+)$", value)
    if m:
        d = next(d for d in inv["disks"] if d["name"] == m.group(1))
        return d["gaps"][0]["id"]
    m = re.match(r"^PART=(.+):(\d+)$", value)
    if m:
        d = next(d for d in inv["disks"] if d["name"] == m.group(1))
        return next(p["id"] for p in d["partitions"] if p["number"] == int(m.group(2)))
    return value


def summarize(inv, names):
    for d in inv["disks"]:
        if d["name"] not in names:
            continue
        print(f"  {d['name']} table={d['table']} size={d['size']/2**20:.0f}MiB")
        for s in d["segments"]:
            if s["kind"] == "gap":
                print(f"     GAP start={s['start']/2**20:.0f}MiB size={s['size']/2**20:.0f}MiB")
            else:
                p = next(x for x in d["partitions"] if x["id"] == s["id"])
                print(f"     #{p['number']} start={p['start']/2**20:.0f}MiB size={p['size']/2**20:.0f}MiB fs={p['fs']} label={p['label']!r} type={p['typeName']} dev={p.get('device')} letter={p.get('letter')}")


try:
    for _ in range(80):
        try:
            get("/api/ping")
            break
        except Exception:
            time.sleep(0.25)
    post("/api/helper/start")
    t0 = time.time()
    while time.time() - t0 < 180:
        st = get("/api/status")["helper"]
        if st["state"] in ("ready", "failed"):
            break
        time.sleep(0.5)
    print("helper:", st["state"], st.get("message"))
    vhd = None
    if "--vhd" in sys.argv:
        i = sys.argv.index("--vhd")
        vhd = sys.argv[i + 1]
        size_mb = int(sys.argv[i + 2]) if len(sys.argv) > i + 2 and sys.argv[i + 2].isdigit() else 1024
        r = post("/api/dev/vhd", {"action": "create", "path": vhd, "sizeMB": size_mb})
        print("test disk:", r)
        if r.get("error"):
            sys.exit(1)
        time.sleep(2)
        inv = get("/api/inventory?refresh=1")
        vd = next(d for d in inv["disks"] if d["id"] == r["disk"])
        for op in ops:
            for k in ("disk", "gap", "part"):
                if isinstance(op.get(k), str) and "VHD" in op[k]:
                    op[k] = op[k].replace("VHD", vd["name"])
    inv = get("/api/inventory?refresh=1")
    names = set()
    for op in ops:
        for k in ("disk", "gap", "part"):
            if k in op:
                op[k] = resolve(inv, op[k])
                did = op[k].split(":")[1] if op[k].startswith(("disk:", "part:", "gap:")) else op[k]
                for d in inv["disks"]:
                    if d["id"] == op[k] or any(p["id"] == op[k] for p in d["partitions"]) or any(g["id"] == op[k] for g in d["gaps"]):
                        names.add(d["name"])
    print("before:")
    summarize(inv, names)
    plan = post("/api/ops/plan", {"ops": ops, "hash": inv["hash"]})
    if plan.get("error"):
        print("plan error:", plan["error"])
        sys.exit(1)
    print("plan texts:", plan["texts"])
    print("plan errors:", plan["errors"], "destructive:", plan["destructive"], "warnings:", plan["warnings"])
    for s in plan["steps"]:
        print(f"  step {s['n']} [{s['kind']}] {s['title']}\n      {s['cmd'].replace(chr(10), chr(10) + '      ')}")
    if plan_only or plan["errors"]:
        sys.exit(0 if not plan["errors"] else 1)
    r = post("/api/ops/apply", {"ops": ops, "hash": inv["hash"], "confirm": True})
    print("apply:", r)
    if r.get("error"):
        sys.exit(1)
    seq = 0
    done = False
    t0 = time.time()
    while not done and time.time() - t0 < 1800:
        ev = get(f"/api/ops/events?since={seq}")
        for e in ev["events"]:
            seq = e["seq"]
            if e["type"] == "step":
                print(f"  step {e['n']} {e['state']} {e.get('message') or ''} {str(e.get('ms')) + 'ms' if e.get('ms') else ''}")
            elif e["type"] == "progress":
                print(f"     {e.get('percent')}% {e.get('message') or ''}")
            elif e["type"] == "log" and e.get("cmd"):
                print(f"     $ {e['cmd'][:100].replace(chr(10), ' | ')} -> {e.get('code')}")
                if e.get("code") not in (0, None) and e.get("output"):
                    print("       " + e["output"][-600:].replace("\n", "\n       "))
            elif e["type"] == "error":
                print("  ERROR:", e["message"])
            elif e["type"] == "done":
                print("  done ok=", e["ok"])
                done = True
        time.sleep(0.3)
    inv = get("/api/inventory?refresh=1")
    print("after:")
    summarize(inv, names)
finally:
    try:
        if "--vhd" in sys.argv and "--keep" not in sys.argv:
            print("detach:", post("/api/dev/vhd", {"action": "detach", "path": sys.argv[sys.argv.index("--vhd") + 1]}))
    except Exception as e:
        print("detach failed:", e)
    if proc is not None:
        try:
            post("/api/quit")
        except Exception:
            pass
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        if out.strip():
            print("--- app output:\n" + out[-3000:])
