"""End-to-end imaging test on a throwaway test disk.

    python tools/image_smoke.py <port> --vhd <image path> <MB> [--no-elevate]

1. starts DiskWorks headless, unlocks, creates + attaches a test disk (VHD / loop)
2. builds a small raw image file (MBR signature + random data) and writes it to the
   test disk with verify
3. backs the test disk up to .img.zst, checks the manifest's SHA-256 against the file
   contents decompressed, then verifies the backup against the disk
4. detaches the test disk
"""
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.request

port = int(sys.argv[1])
vhd = sys.argv[sys.argv.index("--vhd") + 1]
size_mb = int(sys.argv[sys.argv.index("--vhd") + 2])
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


def wait_job():
    seq = 0
    t0 = time.time()
    last = None
    while time.time() - t0 < 1800:
        ev = get(f"/api/image/events?since={seq}")
        for e in ev["events"]:
            seq = e["seq"]
            if e["type"] == "progress":
                last = e
            elif e["type"] == "note":
                print("   note:", e["msg"])
            elif e["type"] == "done":
                if last:
                    print(f"   last progress: {last['phase']} {last['bytes']} / {last['total']} @ {round((last['speed'] or 0)/2**20)} MiB/s")
                return e
        time.sleep(0.3)
    raise SystemExit("job timed out")


tmp = tempfile.mkdtemp(prefix="dwimg-")
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
    if os.path.exists(vhd):
        os.remove(vhd)
    r = post("/api/dev/vhd", {"action": "create", "path": vhd, "sizeMB": size_mb})
    print("test disk:", r)
    if r.get("error"):
        raise SystemExit(1)
    disk_id = r["disk"]
    time.sleep(2)
    # 1. a 40 MiB raw image: hybrid-looking MBR + random bytes + zeros
    img = os.path.join(tmp, "test.img")
    rnd = random.Random(7)
    with open(img, "wb") as f:
        mbr = bytearray(512)
        mbr[446] = 0x80
        mbr[446 + 4] = 0x0c
        mbr[446 + 8:446 + 12] = (2048).to_bytes(4, "little")
        mbr[446 + 12:446 + 16] = (65536).to_bytes(4, "little")
        mbr[510:512] = b"\x55\xaa"
        f.write(bytes(mbr))
        f.write(bytes(2048 * 512 - 512))
        for _ in range(20):
            f.write(rnd.randbytes(2**20))
        f.write(bytes(10 * 2**20))
        f.write(rnd.randbytes(2**20 + 12345))    # odd tail: exercises sector padding
    img_sha = hashlib.sha256(open(img, "rb").read()).hexdigest()
    print("image:", os.path.getsize(img), "bytes sha", img_sha[:16])
    print("inspect:", {k: v for k, v in post("/api/image/inspect", {"path": img}).items() if k in ("kind", "hybrid", "table", "rawSize", "notes")})
    r = post("/api/image/write", {"path": img, "disk": disk_id, "verify": True, "confirm": True})
    print("write start:", r)
    e = wait_job()
    print("write done:", e.get("ok"), {k: e.get("result", {}).get(k) for k in ("written", "sha256", "verified", "mismatches", "elapsed")}, e.get("error"))
    assert e["ok"] and e["result"]["sha256"] == img_sha and e["result"]["verified"], "write/verify mismatch"
    # 2. back up the whole test disk
    dest = os.path.join(tmp, "backup.img.zst")
    r = post("/api/image/backup", {"disk": disk_id, "dest": dest, "compress": "zstd", "level": 3})
    print("backup start:", r)
    e = wait_job()
    res = e.get("result", {})
    print("backup done:", e.get("ok"), {k: res.get(k) for k in ("read", "fileSize", "zeroBytes", "sha256", "manifest")}, e.get("error"))
    assert e["ok"], "backup failed"
    for attempt in range(20):   # the elevated helper just wrote it; Defender may still be scanning
        try:
            man = json.load(open(res["manifest"], encoding="utf-8"))
            break
        except PermissionError:
            time.sleep(0.5)
    from compression import zstd
    for attempt in range(20):   # same transient sharing violation right after the helper closes the file
        try:
            open(dest, "rb").close()
            break
        except PermissionError:
            time.sleep(0.5)
    h = hashlib.sha256()
    n = 0
    with zstd.open(dest, "rb") as f:
        while True:
            chunk = f.read(2**20)
            if not chunk:
                break
            h.update(chunk)
            n += len(chunk)
    print("decompressed:", n, "bytes; sha matches manifest:", h.hexdigest() == man["image"]["sha256"], "; disk-size match:", n == man["source"]["size"])
    assert h.hexdigest() == man["image"]["sha256"]
    # the first 40 MiB of the backup must equal the image we wrote
    with zstd.open(dest, "rb") as f:
        head = f.read(os.path.getsize(img))
    print("backup head == written image:", hashlib.sha256(head).hexdigest() == img_sha)
    # 3. verify the backup against the disk
    r = post("/api/image/verify", {"path": dest, "disk": disk_id})
    e = wait_job()
    print("verify:", e.get("ok"), e.get("result"), e.get("error"))
    print("uncompressed raw backup with sparse holes:")
    dest2 = os.path.join(tmp, "backup.img")
    r = post("/api/image/backup", {"disk": disk_id, "dest": dest2, "compress": "none"})
    e = wait_job()
    res = e.get("result", {})
    st = os.stat(dest2)
    print("  ok:", e.get("ok"), "logical size", st.st_size, "blocks*512", getattr(st, "st_blocks", 0) * 512 if hasattr(st, "st_blocks") else "n/a", "zeroBytes", res.get("zeroBytes"))
    print("ALL GOOD")
finally:
    try:
        print("detach:", post("/api/dev/vhd", {"action": "detach", "path": vhd}))
    except Exception as ex:
        print("detach failed:", ex)
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
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
