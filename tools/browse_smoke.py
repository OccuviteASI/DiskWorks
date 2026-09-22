"""Read-only browser (7-Zip) test on a throwaway test disk.

    python tools/browse_smoke.py <port> --vhd <path> <MB> [--fs ntfs|ext4|hfsplus|exfat] [--copy <dest dir>] [--keep]

Starts DiskWorks headless, unlocks (set DISKWORKS_NO_ELEVATE=1 to skip UAC/pkexec when already
root/admin), creates a test disk with one partition of the given filesystem, opens it through
the Access ladder to drop a few files on it (skipped when this OS cannot mount that filesystem),
closes it again, then lists the partition through /api/access/browse and optionally copies a
file out.  Prints every step; exits 1 on the first failure.  The test disk is detached at
the end unless --keep is given.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

port = int(sys.argv[1])
i = sys.argv.index("--vhd")
vhd, size_mb = sys.argv[i + 1], int(sys.argv[i + 2])
fs = sys.argv[sys.argv.index("--fs") + 1] if "--fs" in sys.argv else "ntfs"
dest = sys.argv[sys.argv.index("--copy") + 1] if "--copy" in sys.argv else None
keep = "--keep" in sys.argv
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
proc = subprocess.Popen([sys.executable, os.path.join(here, "diskworks.py"), "--no-open", "--port", str(port)],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
base = f"http://127.0.0.1:{port}"


def get(p):
    with urllib.request.urlopen(base + p, timeout=120) as r:
        return json.loads(r.read().decode())


def post(p, b=None, timeout=600):
    req = urllib.request.Request(base + p, data=json.dumps(b or {}).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def wait_job():
    since = 0
    while True:
        ev = get(f"/api/ops/events?since={since}")
        since = ev["seq"]
        for x in ev["events"]:
            if x["type"] == "step":
                print(f"    step {x['n']} {x['state']} {x.get('title') or ''} {x.get('message') or ''}")
            if x["type"] == "error":
                fail(x["message"])
            if x["type"] == "done":
                return x["ok"]
        time.sleep(0.4)


def write_files(root):
    d = os.path.join(root, "smoke folder")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(root, "hello.txt"), "w", encoding="utf-8") as f:
        f.write("hello from DiskWorks browse_smoke\n")
    with open(os.path.join(d, "random.bin"), "wb") as f:
        f.write(os.urandom(3 * 1024 * 1024))
    with open(os.path.join(d, "notes.md"), "w", encoding="utf-8") as f:
        f.write("# notes\n" * 100)
    for attempt in range(20):
        try:
            os.sync() if hasattr(os, "sync") else None
            break
        except OSError:
            time.sleep(0.2)


disk_id = None
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
    print("helper:", st["state"], st.get("message") or "")
    if st["state"] != "ready":
        fail("helper not ready")
    acc = get("/api/access/state")
    print("7-Zip available:", acc.get("sevenzip"))

    r = post("/api/dev/vhd", {"action": "create", "path": vhd, "sizeMB": size_mb})
    print("test disk:", r)
    if r.get("error"):
        fail(r["error"])
    disk_id = r["disk"]
    time.sleep(2)
    inv = get("/api/inventory?refresh=1")
    vd = next(d for d in inv["disks"] if d["id"] == disk_id)
    ops = [{"op": "table", "disk": disk_id, "table": "gpt"}]
    plan = post("/api/ops/plan", {"ops": ops, "hash": inv["hash"]})
    if plan.get("errors"):
        fail(plan["errors"][0]["message"])
    gap = plan["preview"]["disks"][[d["id"] for d in plan["preview"]["disks"]].index(disk_id)]["gaps"][0]["id"]
    ops.append({"op": "create", "gap": gap, "fs": fs, "label": "SMOKE"})
    plan = post("/api/ops/plan", {"ops": ops, "hash": inv["hash"]})
    if plan.get("errors"):
        fail(plan["errors"][0]["message"])
    print("plan:", plan["texts"])
    r = post("/api/ops/apply", {"ops": ops, "hash": inv["hash"], "confirm": True})
    if r.get("error"):
        fail(r["error"])
    if not wait_job():
        fail("apply failed")
    inv = get("/api/inventory?refresh=1")
    vd = next(d for d in inv["disks"] if d["id"] == disk_id)
    part = vd["partitions"][-1]
    print(f"partition: {part['id']} fs={part['fs']} label={part.get('label')!r} letter={part.get('letter')} mounts={part.get('mountpoints')}")

    # put files on it through whatever rung this OS has
    lad = post("/api/access/ladder", {"part": part["id"]})
    print("ladder:", [(x["id"], x["state"]) for x in lad["rungs"]])
    root = None
    if part.get("letter"):
        root = part["letter"] + ":\\"
    elif part.get("mountpoints"):
        root = part["mountpoints"][0]
    else:
        rung = next((x for x in lad["rungs"] if x["state"] == "ok" and not x.get("browse")), None)
        if rung:
            r = post("/api/access/open", {"part": part["id"], "rung": rung["id"]})
            print("open:", r)
            if not r.get("error"):
                root = r.get("path")
    if root:
        for attempt in range(30):
            if os.path.isdir(root):
                break
            time.sleep(0.5)
        write_files(root)
        print("files written to", root)
        time.sleep(1)
        r = post("/api/access/close", {"part": part["id"]})
        print("close:", r)
        time.sleep(2)
        get("/api/inventory?refresh=1")
    else:
        print("this OS cannot mount", fs, "- browsing the empty filesystem only")

    # the actual test: list through 7-Zip
    t0 = time.time()
    r = post("/api/access/browse", {"part": part["id"], "path": ""}, timeout=1800)
    if r.get("error"):
        fail("browse: " + r["error"])
    print(f"browse root via {r.get('source')} in {time.time() - t0:.1f}s: {[(e['name'], e['dir'], e['size']) for e in r['entries']]}")
    names = {e["name"] for e in r["entries"]}
    if root and not {"hello.txt", "smoke folder"} <= names:
        fail("expected files not listed")
    if root:
        r2 = post("/api/access/browse", {"part": part["id"], "path": "smoke folder"}, timeout=600)
        if r2.get("error"):
            fail("browse folder: " + r2["error"])
        print("browse 'smoke folder':", [(e["name"], e["size"]) for e in r2["entries"]])
        if {e["name"] for e in r2["entries"]} != {"random.bin", "notes.md"}:
            fail("folder listing wrong")
    if dest and root:
        os.makedirs(dest, exist_ok=True)
        r = post("/api/access/copyout", {"part": part["id"], "paths": ["hello.txt", "smoke folder"], "dest": dest})
        if r.get("error"):
            fail("copyout: " + r["error"])
        since = 0
        while True:
            ev = get(f"/api/access/events?since={since}")
            since = ev["seq"]
            done = [x for x in ev["events"] if x["type"] == "done"]
            if done:
                print("copyout:", done[0])
                if not done[0]["ok"]:
                    fail(done[0].get("error"))
                break
            time.sleep(0.4)
        got = sorted(os.path.relpath(os.path.join(dp, f), dest) for dp, _, fs_ in os.walk(dest) for f in fs_)
        print("copied:", got)
        if os.path.getsize(os.path.join(dest, "smoke folder", "random.bin")) != 3 * 1024 * 1024:
            fail("copied file size wrong")
    print("PASS")
finally:
    if disk_id and not keep:
        try:
            print("detach:", post("/api/dev/vhd", {"action": "detach", "path": vhd}))
        except Exception as e:
            print("detach failed:", e)
    try:
        post("/api/quit")
    except Exception:
        pass
    try:
        proc.wait(timeout=15)
    except Exception:
        proc.kill()
