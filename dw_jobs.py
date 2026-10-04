"""Window-process job manager: the pending-operations plan/apply routes, the
filesystem probe, and (later) imaging and access jobs.  One job at a time; progress is
published through an EventLog the UI polls (ARCHITECTURE.md §3)."""
from __future__ import annotations

import threading
import time

import dw_inventory as di
import dw_ops
from diskworks import EventLog


class Jobs:
    def __init__(self, app):
        self.app = app
        self.events = EventLog(4000)
        self.state: dict = {"running": False, "kind": None, "steps": [], "current": None, "done": False,
                            "error": None, "started": None, "finished": None}
        self.thread: threading.Thread | None = None
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.image = None
        try:
            import dw_image
            self.image = dw_image.ImageJobs(self)
        except ImportError:
            pass
        self.access = None
        try:
            import dw_access
            self.access = dw_access.Access(self)
        except ImportError:
            pass
        # unprivileged tools that live in the window process
        self.speed = None
        self.space = None
        try:
            import dw_speed
            self.speed = dw_speed.SpeedTest(app)
        except ImportError:
            pass
        try:
            import dw_space
            self.space = dw_space.SpaceScan(app)
        except ImportError:
            pass
        # drive health: window-side cache + routes; the reads run in the helper
        self.smart = None
        try:
            import dw_smart
            self.smart = dw_smart.Smart(self)
        except ImportError:
            pass

    # -- lifecycle ------------------------------------------------------------
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def cancel_all(self) -> None:
        self.cancel.set()
        if self.app.helper:
            self.app.helper.cancel()

    def push(self, ev: dict) -> None:
        self.events.push(ev)

    def helper(self):
        if not self.app.helper_ready():
            raise RuntimeError("Administrator rights are needed to change disks. Press Unlock first.")
        return self.app.helper

    # -- routes ----------------------------------------------------------------
    def handle_get(self, h, path: str, q: dict) -> bool:
        if path == "/api/ops/events":
            since = int(q.get("since", ["0"])[0])
            h._json({"events": self.events.since(since), "seq": self.events.seq, "state": self.state})
            return True
        if path == "/api/ops/state":
            h._json(self.state)
            return True
        if self.image and self.image.handle_get(h, path, q):
            return True
        if self.access and self.access.handle_get(h, path, q):
            return True
        if self.speed and self.speed.handle_get(h, path, q):
            return True
        if self.space and self.space.handle_get(h, path, q):
            return True
        if self.smart and self.smart.handle_get(h, path, q):
            return True
        return False

    def handle_post(self, h, path: str, body: dict) -> bool:
        if path == "/api/ops/plan":
            h._json(self.plan(body))
            return True
        if path == "/api/ops/apply":
            h._json(self.apply(body))
            return True
        if path == "/api/ops/cancel":
            self.cancel_all()
            h._json({"ok": True})
            return True
        if path == "/api/fs/probe":
            h._json(self.probe(body))
            return True
        if path == "/api/dev/vhd":
            # Test disks (a VHD on Windows, a loop device on Linux) for trying operations safely.
            r = self.helper().request("vhd", {"action": body.get("action"), "path": body.get("path"), "sizeMB": body.get("sizeMB")},
                                      timeout=300, on_event=self._forward_log)
            self.app.inv_wanted.set()
            time.sleep(1.0)
            h._json(r)
            return True
        if self.image and self.image.handle_post(h, path, body):
            return True
        if self.access and self.access.handle_post(h, path, body):
            return True
        if self.speed and self.speed.handle_post(h, path, body):
            return True
        if self.space and self.space.handle_post(h, path, body):
            return True
        if self.smart and self.smart.handle_post(h, path, body):
            return True
        return False

    # -- planning --------------------------------------------------------------
    def plan(self, body: dict) -> dict:
        ops = body.get("ops")
        if not isinstance(ops, list):
            raise ValueError("ops must be a list")
        inv = self.app.current_inventory()
        res = dw_ops.plan(inv, ops, di.PLATFORM)
        res["hashOk"] = (body.get("hash") in (None, inv.get("hash")))
        res["hash"] = inv.get("hash")
        return res

    def probe(self, body: dict) -> dict:
        inv = self.app.current_inventory()
        part_id = str(body.get("part"))
        for d in inv["disks"]:
            for p in d["partitions"]:
                if p["id"] == part_id:
                    nxt = [q for q in d["partitions"] if q["start"] > p["start"]]
                    limit = min(q["start"] for q in nxt) if nxt else dw_ops.usable_end(d)
                    out = {"minSize": None, "maxSize": limit - p["start"], "used": p.get("used"), "note": "", "probed": False}
                    if self.app.helper_ready():
                        try:
                            r = self.helper().request("probe", {"disk": d["id"], "start": p["start"]}, timeout=900,
                                                      on_event=self._forward_log)
                            out.update({k: r.get(k) for k in ("minSize", "maxSize", "used", "note") if r.get(k) is not None})
                            out["probed"] = True
                        except RuntimeError as e:
                            out["note"] = str(e)
                    else:
                        out["note"] = "Unlock to find the exact minimum size; until then the used space is the lower limit."
                    if out.get("minSize") is None:
                        out["minSize"] = int(p.get("used") or 0) or dw_ops.ALIGN * 4
                    return out
        raise RuntimeError("That partition is no longer present.")

    def _forward_log(self, msg: dict) -> None:
        if msg.get("event") == "log":
            if msg.get("cmd"):
                self.app.log_cmd(msg["cmd"], msg.get("code"), msg.get("ms") or 0, msg.get("output") or "", msg.get("title") or "")
            elif msg.get("msg"):
                self.app.info("[helper] " + msg["msg"])

    # -- apply -----------------------------------------------------------------
    def apply(self, body: dict) -> dict:
        with self.lock:
            if self.running():
                raise RuntimeError("Another job is still running.")
            ops = body.get("ops")
            if not isinstance(ops, list) or not ops:
                raise ValueError("Nothing to apply.")
            inv = self.app.current_inventory()
            if body.get("hash") and body["hash"] != inv.get("hash"):
                raise RuntimeError("The disks changed since you planned these operations. Review the queue and try again.")
            res = dw_ops.plan(inv, ops, di.PLATFORM)
            if res["errors"]:
                raise RuntimeError(res["errors"][0]["message"])
            if res["destructive"] and not body.get("confirm"):
                raise RuntimeError("This queue destroys data; confirm it first.")
            helper = self.helper()
            steps = res["steps"]
            self.cancel.clear()
            self.events.clear()
            self.state = {"running": True, "kind": "ops", "steps": [{"n": s["n"], "title": s["title"], "cmd": s["cmd"], "state": "wait"} for s in steps],
                          "current": None, "done": False, "error": None, "started": time.time(), "finished": None}
            self.push({"type": "start", "steps": self.state["steps"], "texts": res["texts"]})
            self.thread = threading.Thread(target=self._run_ops, args=(helper, steps, inv.get("hash")), daemon=True, name="ops-apply")
            self.thread.start()
            return {"ok": True, "steps": len(steps)}

    def _run_ops(self, helper, steps: list[dict], hash_before: str | None) -> None:
        ok = True
        for i, s in enumerate(steps):
            if self.cancel.is_set():
                self.push({"type": "error", "message": "Cancelled before step %d." % s["n"]})
                ok = False
                break
            self.state["current"] = s["n"]
            self.state["steps"][i]["state"] = "run"
            self.push({"type": "step", "n": s["n"], "state": "run", "title": s["title"], "cmd": s["cmd"]})
            t0 = time.time()

            def on_event(msg, n=s["n"]):
                if msg.get("event") == "progress":
                    self.push({"type": "progress", "n": n, "percent": msg.get("percent"), "message": msg.get("message")})
                else:
                    self._forward_log(msg)
                    if msg.get("event") == "log":
                        self.push({"type": "log", "n": n, "title": msg.get("title"), "cmd": msg.get("cmd"), "code": msg.get("code"),
                                   "output": (msg.get("output") or "")[-4000:], "msg": msg.get("msg")})
            try:
                helper.request("step", {"step": s, "hash": hash_before if i == 0 else None}, on_event=on_event, timeout=48 * 3600)
                self.state["steps"][i]["state"] = "done"
                self.push({"type": "step", "n": s["n"], "state": "done", "ms": round((time.time() - t0) * 1000)})
            except RuntimeError as e:
                self.state["steps"][i]["state"] = "fail"
                self.state["error"] = str(e)
                self.push({"type": "step", "n": s["n"], "state": "fail", "message": str(e)})
                self.push({"type": "error", "message": str(e), "n": s["n"]})
                ok = False
                break
        # refresh the inventory now that the disks changed
        before = (self.app.inv or {}).get("ts")
        self.app.inv_wanted.set()
        t0 = time.time()
        while time.time() - t0 < 30 and (self.app.inv or {}).get("ts") == before:
            time.sleep(0.2)
        self.state.update({"running": False, "done": True, "finished": time.time(), "current": None})
        self.push({"type": "done", "ok": ok, "hash": (self.app.inv or {}).get("hash")})
        self.app.info("Operations finished" if ok else "Operations stopped: " + (self.state.get("error") or ""))
