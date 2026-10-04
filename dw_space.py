"""Space browser (DaisyDisk rings + WizTree treemap): scan a volume or folder, keep a
size tree in memory, serve it level by level for the rings and the treemap, keep the
largest files and the per-file-type totals of the whole scan, delete what the user picks
(to the Recycle Bin / Trash by default, permanently on request) and reveal items in the
file manager.  Runs in the window process with the user's own permissions; folders it
may not read are counted as skipped.
"""
from __future__ import annotations

import heapq
import os
import shutil
import stat
import subprocess
import sys
import threading
import time

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
TOP_FILES = 40            # largest files remembered per folder; the rest is "other files"
LARGEST = 1000            # largest files remembered for the whole scan (WizTree's "File view")
TYPES_SHOWN = 60          # file types listed; the rest is aggregated
TREE_CHILDREN = 60        # subfolders exported per level for the rings
TREEMAP_CHILDREN = 400    # ... and for the treemap (its tiles are pruned by size instead)
CRITICAL = [
    "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)", "C:\\ProgramData", "C:\\Users", "C:\\$Recycle.Bin",
    "/System", "/Library", "/usr", "/bin", "/sbin", "/etc", "/var", "/private", "/Applications", "/Users", "/boot", "/lib", "/lib64", "/proc", "/sys", "/dev", "/home",
]


class Node:
    __slots__ = ("name", "size", "files", "dirs", "children", "top", "other", "otherCount", "skipped", "parent")

    def __init__(self, name: str, parent: "Node | None"):
        self.name = name
        self.size = 0
        self.files = 0
        self.dirs = 0
        self.children: dict[str, Node] = {}
        self.top: list[tuple[str, int, float]] = []
        self.other = 0
        self.otherCount = 0
        self.skipped = 0
        self.parent = parent

    def path(self, root: str) -> str:
        parts = []
        n = self
        while n.parent is not None:
            parts.append(n.name)
            n = n.parent
        return os.path.join(root, *reversed(parts)) if parts else root


class SpaceScan:
    def __init__(self, app):
        self.app = app
        self.events = app.log.__class__(4000)
        self.state: dict = {"running": False, "root": None, "dirs": 0, "files": 0, "bytes": 0, "skipped": 0, "current": "",
                            "done": False, "error": None, "started": None, "finished": None}
        self.root: str | None = None
        self.tree: Node | None = None
        self.types: dict[str, list] = {}          # ext -> [bytes, count]
        self.largest: list[tuple[int, float, str]] = []   # min-heap of (size, mtime, rel path)
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None

    # -- routes ------------------------------------------------------------------
    def handle_get(self, h, path: str, q: dict) -> bool:
        if path == "/api/space/targets":
            h._json({"targets": self.targets()})
            return True
        if path == "/api/space/events":
            since = int(q.get("since", ["0"])[0])
            h._json({"events": self.events.since(since), "seq": self.events.seq, "state": self.state})
            return True
        if path == "/api/space/tree":
            rel = q.get("path", [""])[0]
            depth = max(1, min(5, int(q.get("depth", ["2"])[0])))
            min_frac = float(q.get("min", ["0"])[0])        # prune children below this share of the node (treemap)
            limit = TREEMAP_CHILDREN if min_frac > 0 else TREE_CHILDREN
            h._json(self.subtree(rel, depth, min_frac, limit))
            return True
        if path == "/api/space/types":
            h._json(self.file_types())
            return True
        if path == "/api/space/largest":
            n = max(1, min(LARGEST, int(q.get("n", ["200"])[0])))
            h._json(self.largest_files(n))
            return True
        return False

    def handle_post(self, h, path: str, body: dict) -> bool:
        if path == "/api/space/scan":
            h._json(self.scan(str(body.get("root") or "")))
            return True
        if path == "/api/space/stop":
            self.stop_flag.set()
            h._json({"ok": True})
            return True
        if path == "/api/space/delete":
            h._json(self.delete([str(p) for p in body.get("paths") or []], str(body.get("mode") or "trash")))
            return True
        if path == "/api/space/reveal":
            h._json(self.reveal(str(body.get("path") or "")))
            return True
        return False

    def targets(self) -> list[dict]:
        inv = self.app.current_inventory()
        out = []
        for d in inv.get("disks", []):
            for p in d.get("partitions", []):
                roots = []
                if p.get("letter"):
                    roots.append(f"{p['letter']}:\\")
                roots += [m for m in (p.get("mountpoints") or []) if not m.endswith(":\\")]
                for root in roots:
                    if not os.path.isdir(root):
                        continue
                    out.append({"id": p["id"], "root": root, "title": p.get("label") or "", "disk": d["name"], "fs": p.get("fs"),
                                "size": p.get("volSize") or p.get("size"), "used": p.get("used"), "free": p.get("free"),
                                "system": bool(d.get("system") or d.get("boot"))})
        home = os.path.expanduser("~")
        if os.path.isdir(home):
            out.append({"id": "home", "root": home, "title": "Your home folder", "disk": "", "fs": None, "size": None, "used": None, "free": None, "system": False})
        return out

    # -- scanning ----------------------------------------------------------------
    def scan(self, root: str) -> dict:
        if self.thread and self.thread.is_alive():
            raise RuntimeError("A scan is already running; stop it first.")
        if not root or not os.path.isdir(root):
            raise RuntimeError("Pick a drive or folder to scan.")
        root = os.path.abspath(root)
        self.stop_flag.clear()
        self.events.clear()
        with self.lock:
            self.root = root
            self.tree = Node("", None)
            self.types = {}
            self.largest = []
        self.state = {"running": True, "root": root, "dirs": 0, "files": 0, "bytes": 0, "skipped": 0, "current": root,
                      "done": False, "error": None, "started": time.time(), "finished": None}
        self.thread = threading.Thread(target=self._scan, args=(root,), daemon=True, name="spacescan")
        self.thread.start()
        self.app.info(f"Space scan started: {root}")
        return {"ok": True, "root": root}

    def _scan(self, root: str) -> None:
        tree = self.tree
        types = self.types
        largest = self.largest
        last = time.time()
        dirs = files = total = skipped = 0
        stack: list[tuple[str, Node]] = [(root, tree)]
        rootlen = len(root.rstrip("\\/")) + 1
        try:
            while stack and not self.stop_flag.is_set():
                path, node = stack.pop()
                try:
                    it = os.scandir(path)
                except OSError:
                    node.skipped += 1
                    skipped += 1
                    continue
                with it:
                    for e in it:
                        try:
                            if e.is_symlink():
                                continue
                            st = e.stat(follow_symlinks=False)
                            if IS_WIN and (getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                                continue   # junctions and OneDrive placeholders: never follow
                            if stat.S_ISDIR(st.st_mode):
                                child = Node(e.name, node)
                                node.children[e.name] = child
                                node.dirs += 1
                                dirs += 1
                                stack.append((e.path, child))
                            elif stat.S_ISREG(st.st_mode):
                                sz = st.st_size
                                files += 1
                                total += sz
                                dot = e.name.rfind(".")
                                ext = e.name[dot + 1:].lower() if 0 < dot < len(e.name) - 1 and len(e.name) - dot <= 12 else ""
                                if ext.isdigit():       # libfoo.so.1, backup.2: a version, not a type
                                    ext = ""
                                t = types.get(ext)
                                if t is None:
                                    types[ext] = [sz, 1]
                                else:
                                    t[0] += sz
                                    t[1] += 1
                                if len(largest) < LARGEST:
                                    heapq.heappush(largest, (sz, st.st_mtime, e.path[rootlen:]))
                                elif sz > largest[0][0]:
                                    heapq.heapreplace(largest, (sz, st.st_mtime, e.path[rootlen:]))
                                n = node
                                while n is not None:
                                    n.size += sz
                                    n.files += 1
                                    n = n.parent
                                top = node.top
                                if len(top) < TOP_FILES:
                                    top.append((e.name, sz, st.st_mtime))
                                    if len(top) == TOP_FILES:
                                        top.sort(key=lambda t: -t[1])
                                elif sz > top[-1][1]:
                                    dropped = top.pop()
                                    node.other += dropped[1]
                                    node.otherCount += 1
                                    top.append((e.name, sz, st.st_mtime))
                                    top.sort(key=lambda t: -t[1])
                                else:
                                    node.other += sz
                                    node.otherCount += 1
                        except OSError:
                            node.skipped += 1
                            skipped += 1
                now = time.time()
                if now - last >= 0.4:
                    last = now
                    self.state.update({"dirs": dirs, "files": files, "bytes": total, "skipped": skipped, "current": path})
                    self.events.push({"type": "progress", "dirs": dirs, "files": files, "bytes": total, "skipped": skipped, "current": path[-120:]})
            # the per-dir top lists are only partially sorted while filling
            self._sort_tops(tree)
        except Exception as e:
            self.state["error"] = str(e)
            self.events.push({"type": "error", "message": str(e)})
        finally:
            self.state.update({"running": False, "done": True, "dirs": dirs, "files": files, "bytes": total, "skipped": skipped,
                               "finished": time.time(), "stopped": self.stop_flag.is_set()})
            self.events.push({"type": "done", "dirs": dirs, "files": files, "bytes": total, "skipped": skipped, "stopped": self.stop_flag.is_set()})
            self.app.info(f"Space scan {'stopped' if self.stop_flag.is_set() else 'finished'}: {files:,} files, {total:,} bytes in {root}")

    def _sort_tops(self, node: Node) -> None:
        stack = [node]
        while stack:
            n = stack.pop()
            n.top.sort(key=lambda t: -t[1])
            stack.extend(n.children.values())

    # -- serving -----------------------------------------------------------------
    def _node_at(self, rel: str) -> Node | None:
        n = self.tree
        for part in [p for p in rel.replace("\\", "/").split("/") if p]:
            n = n.children.get(part) if n else None
            if n is None:
                return None
        return n

    def subtree(self, rel: str, depth: int = 2, min_frac: float = 0.0, limit: int = TREE_CHILDREN) -> dict:
        with self.lock:
            if self.tree is None or self.root is None:
                raise RuntimeError("Nothing scanned yet.")
            node = self._node_at(rel)
            if node is None:
                raise RuntimeError("That folder is no longer in the scan.")
            floor = int(node.size * min_frac) if min_frac > 0 else 0
            return {"root": self.root, "path": rel, "node": self._export(node, depth, rel, floor, limit), "state": self.state}

    def _export(self, node: Node, depth: int, rel: str, floor: int = 0, limit: int = TREE_CHILDREN) -> dict:
        """One level of the tree. `floor` (bytes) prunes subfolders and files too small to draw: what
        is pruned is summed into `pruned` so the shares still add up (the treemap asks with a floor)."""
        d = {"name": node.name, "rel": rel, "size": node.size, "files": node.files, "dirs": node.dirs, "skipped": node.skipped,
             "other": node.other, "otherCount": node.otherCount,
             "top": [{"name": n, "size": s, "mtime": m, "rel": (rel + "/" if rel else "") + n} for n, s, m in node.top[:TOP_FILES] if s >= floor],
             "children": []}
        kids = sorted(node.children.values(), key=lambda c: -c.size)
        shown = [c for c in kids[:limit] if c.size >= floor]
        for c in shown:
            crel = (rel + "/" if rel else "") + c.name
            d["children"].append(self._export(c, depth - 1, crel, floor, limit) if depth > 1 else
                                 {"name": c.name, "rel": crel, "size": c.size, "files": c.files, "dirs": c.dirs, "skipped": c.skipped, "children": None})
        shown_ids = {id(c) for c in shown}
        rest = [c for c in kids if id(c) not in shown_ids]
        if rest:
            d["moreDirs"] = {"count": len(rest), "size": sum(c.size for c in rest)}
        if floor:
            d["prunedFiles"] = sum(s for _, s, _ in node.top if s < floor)
        return d

    def file_types(self) -> dict:
        """Totals per file extension over the whole scan (WizTree's "File types" view)."""
        with self.lock:
            if self.tree is None or self.root is None:
                raise RuntimeError("Nothing scanned yet.")
            rows = sorted(((ext, v[0], v[1]) for ext, v in self.types.items()), key=lambda r: -r[1])
            total = sum(r[1] for r in rows)
            out = [{"ext": ext, "size": size, "count": count} for ext, size, count in rows[:TYPES_SHOWN]]
            rest = rows[TYPES_SHOWN:]
            if rest:
                out.append({"ext": None, "size": sum(r[1] for r in rest), "count": sum(r[2] for r in rest), "types": len(rest)})
            return {"root": self.root, "total": total, "types": out, "running": bool(self.state.get("running"))}

    def largest_files(self, n: int) -> dict:
        with self.lock:
            if self.tree is None or self.root is None:
                raise RuntimeError("Nothing scanned yet.")
            top = heapq.nlargest(n, self.largest)
            return {"root": self.root, "files": [{"rel": rel.replace("\\", "/"), "name": os.path.basename(rel), "size": size, "mtime": mtime} for size, mtime, rel in top],
                    "total": self.tree.size, "running": bool(self.state.get("running"))}

    # -- deleting ------------------------------------------------------------------
    def _abs(self, rel: str) -> str:
        p = os.path.abspath(os.path.join(self.root, *[x for x in rel.replace("\\", "/").split("/") if x]))
        if os.path.commonpath([p, self.root]) != os.path.abspath(self.root) or p == os.path.abspath(self.root):
            raise RuntimeError("Only items inside the scanned folder can be deleted, and not the folder itself.")
        return p

    def delete(self, rels: list[str], mode: str) -> dict:
        if self.tree is None or self.root is None:
            raise RuntimeError("Nothing scanned yet.")
        if not rels:
            raise RuntimeError("Nothing selected.")
        paths = [self._abs(r) for r in rels]
        for p in paths:
            for c in CRITICAL:
                if os.path.normcase(p.rstrip("\\/")) == os.path.normcase(c.rstrip("\\/")):
                    raise RuntimeError(f"{p} is part of the operating system; DiskWorks will not delete it.")
        freed = 0
        results = []
        errors = []
        for rel, p in zip(rels, paths):
            node = self._node_at(rel)
            size = node.size if node else (os.path.getsize(p) if os.path.isfile(p) else 0)
            try:
                if mode == "permanent":
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
                else:
                    trash(p)
                freed += size
                results.append(p)
                self._forget(rel, size)
                self._forget_largest(rel)
            except Exception as e:
                errors.append(f"{p}: {e}")
        self.app.info(f"Deleted {len(results)} item(s) ({freed:,} bytes) {'permanently' if mode == 'permanent' else 'to the Recycle Bin / Trash'}"
                      + (f"; {len(errors)} failed" if errors else ""))
        return {"ok": not errors, "deleted": results, "freed": freed, "errors": errors}

    def _forget(self, rel: str, size: int) -> None:
        """Remove a deleted item from the tree and subtract its size up the chain."""
        with self.lock:
            parts = [p for p in rel.replace("\\", "/").split("/") if p]
            if not parts:
                return
            parent = self._node_at("/".join(parts[:-1]))
            if parent is None:
                return
            name = parts[-1]
            child = parent.children.pop(name, None)
            if child is not None:
                files = child.files
                parent.dirs -= 1
            else:
                idx = next((i for i, t in enumerate(parent.top) if t[0] == name), None)
                if idx is not None:
                    parent.top.pop(idx)
                files = 1
            n = parent
            while n is not None:
                n.size = max(0, n.size - size)
                n.files = max(0, n.files - files)
                n = n.parent

    def _forget_largest(self, rel: str) -> None:
        """Drop a deleted file (or everything under a deleted folder) from the largest-files list."""
        with self.lock:
            key = rel.replace("\\", "/").strip("/")
            keep = [t for t in self.largest if not (t[2].replace("\\", "/") == key or t[2].replace("\\", "/").startswith(key + "/"))]
            if len(keep) != len(self.largest):
                heapq.heapify(keep)
                self.largest = keep

    def reveal(self, rel: str) -> dict:
        p = self._abs(rel) if rel else self.root
        if IS_WIN:
            subprocess.Popen(["explorer", "/select,", p] if os.path.isfile(p) else ["explorer", p])
        elif IS_MAC:
            subprocess.Popen(["/usr/bin/open", "-R", p])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(p) if os.path.isfile(p) else p])
        return {"ok": True}


# ----------------------------------------------------------------------------
# Trash
# ----------------------------------------------------------------------------
def trash(path: str) -> None:
    if IS_WIN:
        import base64
        script = ("Add-Type -AssemblyName Microsoft.VisualBasic\n"
                  "$p = $env:DW_TRASH_PATH\n"
                  "if (Test-Path -LiteralPath $p -PathType Container) { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($p, 'OnlyErrorDialogs', 'SendToRecycleBin') }"
                  " else { [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p, 'OnlyErrorDialogs', 'SendToRecycleBin') }")
        enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                           capture_output=True, text=True, timeout=600, env=dict(os.environ, DW_TRASH_PATH=path), creationflags=0x08000000)
        if r.returncode != 0 or os.path.exists(path):
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "the Recycle Bin refused it")
        return
    if IS_MAC:
        esc = path.replace("\\", "\\\\").replace('"', '\\"')
        r = subprocess.run(["/usr/bin/osascript", "-e", f'tell application "Finder" to delete POSIX file "{esc}"'], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip() or "Finder refused")
        return
    if shutil.which("gio"):
        r = subprocess.run(["gio", "trash", path], capture_output=True, text=True, timeout=600)
        if r.returncode == 0:
            return
    freedesktop_trash(path)


def freedesktop_trash(path: str) -> None:
    """Move to the XDG trash: the home trash when on the same filesystem, else <mount>/.Trash-<uid>."""
    import urllib.parse
    home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    trash_dir = os.path.join(home, "Trash")
    if os.stat(path).st_dev != os.stat(os.path.expanduser("~")).st_dev:
        mnt = path
        while not os.path.ismount(mnt) and os.path.dirname(mnt) != mnt:
            mnt = os.path.dirname(mnt)
        trash_dir = os.path.join(mnt, f".Trash-{os.getuid()}")
    files_dir, info_dir = os.path.join(trash_dir, "files"), os.path.join(trash_dir, "info")
    os.makedirs(files_dir, mode=0o700, exist_ok=True)
    os.makedirs(info_dir, mode=0o700, exist_ok=True)
    base = os.path.basename(path.rstrip("/"))
    name, n = base, 1
    while os.path.exists(os.path.join(files_dir, name)) or os.path.exists(os.path.join(info_dir, name + ".trashinfo")):
        n += 1
        name = f"{base}.{n}"
    with open(os.path.join(info_dir, name + ".trashinfo"), "w", encoding="utf-8") as f:
        f.write("[Trash Info]\nPath=%s\nDeletionDate=%s\n" % (urllib.parse.quote(os.path.abspath(path)), time.strftime("%Y-%m-%dT%H:%M:%S")))
    shutil.move(path, os.path.join(files_dir, name))
