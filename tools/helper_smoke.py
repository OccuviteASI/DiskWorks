"""Start DiskWorks headless on a port, ask it to start the helper, wait until ready,
print the refined inventory summary, then quit.  Set DISKWORKS_NO_ELEVATE=1 to skip UAC/pkexec."""
import json
import os
import subprocess
import sys
import time
import urllib.request

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
proc = subprocess.Popen([sys.executable, os.path.join(here, "diskworks.py"), "--no-open", "--port", str(port)],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
base = f"http://127.0.0.1:{port}"


def get(path):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read().decode())


def post(path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


try:
    for _ in range(60):
        try:
            get("/api/ping")
            break
        except Exception:
            time.sleep(0.25)
    print("status:", json.dumps(get("/api/status")["helper"]))
    print("helper/start:", json.dumps(post("/api/helper/start")))
    t0 = time.time()
    st = None
    while time.time() - t0 < 180:
        st = get("/api/status")["helper"]
        if st["state"] in ("ready", "failed", "absent"):
            if st["state"] != "absent" or time.time() - t0 > 5:
                break
        time.sleep(0.5)
    print(f"helper after {time.time()-t0:.1f}s:", json.dumps(st))
    inv = get("/api/inventory?refresh=1")
    print("inventory refined:", inv.get("refined"), "elevated:", inv.get("elevated"), "hash:", inv.get("hash"), "wsl:", json.dumps(inv.get("wsl"))[:200])
    for d in inv["disks"]:
        print(f"  {d['name']} table={d['table']} refineError={d.get('refineError')}")
        for p in d["partitions"]:
            print(f"     {p['id']} fs={p['fs']} src={p.get('fsSource')} detail={p.get('fsDetail')} bitlocker={p.get('bitlocker')} locked={p['locked']}")
    log = get("/api/log?since=0")["events"]
    print("log tail:")
    for e in log[-6:]:
        print("  ", e.get("msg") or (e.get("title", "") + " $ " + e.get("cmd", "")[:80]))
finally:
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
