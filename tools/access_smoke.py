"""Access-ladder test on a loop/VHD test disk: create an ext4 partition, evaluate the ladder,
open (mount) it, list the mount point, close it.  python tools/access_smoke.py <port> --vhd <path> <MB>"""
import json, os, subprocess, sys, time, urllib.request
port = int(sys.argv[1]); vhd = sys.argv[sys.argv.index("--vhd") + 1]; size_mb = int(sys.argv[sys.argv.index("--vhd") + 2])
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
proc = subprocess.Popen([sys.executable, os.path.join(here, "diskworks.py"), "--no-open", "--port", str(port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
base = f"http://127.0.0.1:{port}"
def get(p):
    with urllib.request.urlopen(base + p, timeout=60) as r: return json.loads(r.read().decode())
def post(p, b=None):
    req = urllib.request.Request(base + p, data=json.dumps(b or {}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r: return json.loads(r.read().decode())
    except urllib.error.HTTPError as e: return json.loads(e.read().decode())
try:
    for _ in range(80):
        try: get("/api/ping"); break
        except Exception: time.sleep(0.25)
    post("/api/helper/start")
    for _ in range(360):
        if get("/api/status")["helper"]["state"] in ("ready", "failed"): break
        time.sleep(0.5)
    print("helper:", get("/api/status")["helper"]["state"])
    if os.path.exists(vhd): os.remove(vhd)
    r = post("/api/dev/vhd", {"action": "create", "path": vhd, "sizeMB": size_mb}); print("test disk:", r); disk = r["disk"]
    time.sleep(2); inv = get("/api/inventory?refresh=1")
    d = next(x for x in inv["disks"] if x["id"] == disk)
    ops = [{"op": "table", "disk": disk, "table": "gpt"}, {"op": "create", "gap": d["gaps"][0]["id"], "disk": disk, "start": 0, "size": 200 * 2**20, "fs": "ext4" if os.name != "nt" else "ntfs", "label": "acc"}]
    r = post("/api/ops/apply", {"ops": ops, "hash": inv["hash"], "confirm": True}); print("apply:", r)
    seq = 0; done = False
    while not done:
        ev = get(f"/api/ops/events?since={seq}")
        for e in ev["events"]:
            seq = e["seq"]
            if e["type"] == "error": print("  ERROR", e["message"])
            if e["type"] == "done": done = True
        time.sleep(0.3)
    inv = get("/api/inventory?refresh=1"); d = next(x for x in inv["disks"] if x["id"] == disk)
    part = [p for p in d["partitions"] if p.get("fs") in ("ext4", "ntfs")][0]
    print("partition:", part["id"], part["fs"], part.get("device"), part.get("letter"))
    ev = post("/api/access/ladder", {"part": part["id"]}); print("summary:", ev.get("summary") or ev.get("error"))
    for rg in ev.get("rungs", []): print(f"   [{rg['state']}] {rg['title']}: {rg['why'][:110]}")
    if ev.get("recommended"):
        r = post("/api/access/open", {"part": part["id"], "rung": ev["recommended"]}); print("open:", r)
        if r.get("path") and os.name != "nt":
            print("  mount listing:", os.listdir(r["path"]))
        ev2 = post("/api/access/ladder", {"part": part["id"]}); print("after open:", ev2.get("summary"), "opened=", ev2.get("opened"))
        print("close:", post("/api/access/close", {"part": part["id"]}))
        ev3 = post("/api/access/ladder", {"part": part["id"]}); print("after close:", ev3.get("summary"))
finally:
    print("detach:", post("/api/dev/vhd", {"action": "detach", "path": vhd}))
    post("/api/quit"); proc.wait(timeout=8)
    out = proc.stdout.read()
    if out.strip(): print("--- app:", out[-1500:])
