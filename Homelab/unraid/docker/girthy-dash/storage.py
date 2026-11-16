"""WizTree-style disk usage scanner for the Unraid user shares.

The whole tree (directories + files) is held in memory so any folder can be
drilled into instantly; it is cached gzipped in DATA_DIR so restarts don't need
a rescan. Scanning reads metadata only, but that does spin up array disks.
"""

import gzip
import heapq
import json
import logging
import os
import posixpath
import threading
import time

log = logging.getLogger("girthy-dash.storage")

KINDS = ["video", "image", "audio", "archive", "other"]
EXT_KIND = {}
for _i, _exts in enumerate((
        "mkv mp4 m4v avi mov wmv ts m2ts mts webm mpg mpeg vob flv 3gp",
        "jpg jpeg png gif heic heif webp tif tiff bmp raw cr2 cr3 nef arw dng orf rw2 raf",
        "mp3 flac m4a m4b aac wav ogg opus wma aiff",
        "zip rar 7z tar gz tgz xz bz2 zst iso img qcow2 vmdk vhd vhdx dmg")):
    for _e in _exts.split():
        EXT_KIND[_e] = _i
OTHER = 4


def ext_of(name):
    i = name.rfind(".")
    if 0 < i < len(name) - 1 and len(name) - i <= 8:
        return name[i + 1:].lower()
    return ""


class Dir:
    __slots__ = ("name", "parent", "dirs", "files", "size", "nfiles", "ndirs", "kinds", "mtime", "error")

    def __init__(self, name, parent):
        self.name = name
        self.parent = parent
        self.dirs = {}
        self.files = []          # (name, size, mtime, kind)
        self.size = 0
        self.nfiles = 0
        self.ndirs = 0
        self.kinds = [0] * len(KINDS)
        self.mtime = 0
        self.error = None

    def add_child(self, sub):
        self.dirs[sub.name] = sub
        self.size += sub.size
        self.nfiles += sub.nfiles
        self.ndirs += sub.ndirs + 1
        for i, v in enumerate(sub.kinds):
            self.kinds[i] += v
        if sub.mtime > self.mtime:
            self.mtime = sub.mtime

    def add_file(self, name, size, mtime, kind):
        self.files.append((name, size, mtime, kind))
        self.size += size
        self.nfiles += 1
        self.kinds[kind] += size
        if mtime > self.mtime:
            self.mtime = mtime


def dominant(kinds):
    best = max(range(len(kinds)), key=lambda i: kinds[i])
    return KINDS[best] if kinds[best] > 0 else "other"


class Progress:
    def __init__(self):
        self.files = 0
        self.dirs = 0
        self.bytes = 0
        self.errors = 0
        self.current = ""
        self.started = time.time()
        self.seen = set()       # (dev, ino) of hardlinked files already counted
        self.hardlinks = 0


def scan_dir(path, name, parent, prog, depth=0):
    d = Dir(name, parent)
    if depth <= 2:
        prog.current = path
    try:
        it = os.scandir(path)
    except OSError as e:
        d.error = e.strerror or str(e)
        prog.errors += 1
        return d
    with it:
        for entry in it:
            try:
                if entry.is_dir(follow_symlinks=False):
                    prog.dirs += 1
                    d.add_child(scan_dir(entry.path, entry.name, d, prog, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    size = st.st_size
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in prog.seen:
                            size = 0
                            prog.hardlinks += 1
                        else:
                            prog.seen.add(key)
                    d.add_file(entry.name, size, int(st.st_mtime), EXT_KIND.get(ext_of(entry.name), OTHER))
                    prog.files += 1
                    prog.bytes += size
            except OSError:
                prog.errors += 1
    return d


# ---------------------------------------------------------------- cache (de)serialization

def to_plain(d):
    return [d.name, d.mtime, d.error, [list(f) for f in d.files], [to_plain(c) for c in d.dirs.values()]]


def from_plain(p, parent):
    name, mtime, error, files, dirs = p
    d = Dir(name, parent)
    d.error = error
    for c in dirs:
        d.add_child(from_plain(c, d))
    for f in files:
        d.add_file(f[0], f[1], f[2], f[3])
    d.mtime = max(d.mtime, mtime)
    return d


class Scanner:
    def __init__(self, host_root, display_root, data_dir):
        self.host_root = host_root.rstrip("/")
        self.display_root = display_root.rstrip("/")
        self.cache_file = os.path.join(data_dir, "storage-scan.json.gz")
        self.lock = threading.RLock()
        self.tree = None
        self.info = None
        self.progress = None
        self.error = None

    # -- paths
    def normalize(self, path):
        path = posixpath.normpath("/" + (path or self.display_root).lstrip("/"))
        if path != self.display_root and not path.startswith(self.display_root + "/"):
            raise ValueError(f"path must be under {self.display_root}")
        return path

    def to_host(self, display_path):
        return self.host_root + display_path[len(self.display_root):]

    def find(self, display_path):
        node = self.tree
        rel = display_path[len(self.display_root):].strip("/")
        for part in rel.split("/") if rel else []:
            node = node.dirs.get(part) if node else None
        return node

    # -- scanning
    @property
    def running(self):
        return self.progress is not None

    def start(self):
        with self.lock:
            if self.progress is not None:
                return False
            self.progress = Progress()
        threading.Thread(target=self._run, name="storage-scan", daemon=True).start()
        return True

    def _run(self):
        prog = self.progress
        try:
            tree = scan_dir(self.host_root, self.display_root, None, prog)
            info = {"finished": time.time(), "duration": time.time() - prog.started, "files": tree.nfiles,
                    "dirs": tree.ndirs, "bytes": tree.size, "errors": prog.errors, "hardlinks": prog.hardlinks}
            with self.lock:
                self.tree, self.info, self.error = tree, info, None
            log.info("storage scan done: %d files, %.1f GB in %.1fs",
                     tree.nfiles, tree.size / 1e9, info["duration"])
            self.save()
        except Exception as e:
            log.exception("storage scan failed")
            self.error = str(e)
        finally:
            with self.lock:
                self.progress = None

    def rescan_path(self, display_path):
        """Re-read one folder (after a delete) and fix up the totals above it."""
        display_path = self.normalize(display_path)
        with self.lock:
            if self.tree is None:
                return
            node = self.find(display_path)
            parent_path = posixpath.dirname(display_path)
            parent = self.find(parent_path) if display_path != self.display_root else None
        host = self.to_host(display_path)
        prog = Progress()
        new = scan_dir(host, posixpath.basename(display_path), parent, prog) if os.path.isdir(host) else None
        with self.lock:
            if display_path == self.display_root:
                if new:
                    self.tree = new
                return
            if parent is None or self.find(parent_path) is not parent:
                return
            old = parent.dirs.get(posixpath.basename(display_path))
            if old is not None and old is not node:
                return
            ds = (new.size if new else 0) - (old.size if old else 0)
            df = (new.nfiles if new else 0) - (old.nfiles if old else 0)
            dd = ((new.ndirs + 1) if new else 0) - ((old.ndirs + 1) if old else 0)
            dk = [(new.kinds[i] if new else 0) - (old.kinds[i] if old else 0) for i in range(len(KINDS))]
            if new:
                parent.dirs[new.name] = new
            else:
                parent.dirs.pop(posixpath.basename(display_path), None)
            p = parent
            while p is not None:
                p.size += ds
                p.nfiles += df
                p.ndirs += dd
                for i in range(len(KINDS)):
                    p.kinds[i] += dk[i]
                p = p.parent
            if self.info:
                self.info["bytes"] = self.tree.size
                self.info["files"] = self.tree.nfiles
        self.save()

    # -- persistence
    def save(self):
        with self.lock:
            if self.tree is None:
                return
            payload = {"info": self.info, "root": self.display_root, "tree": to_plain(self.tree)}
        tmp = self.cache_file + ".tmp"
        try:
            with gzip.open(tmp, "wt", compresslevel=3) as f:
                json.dump(payload, f, separators=(",", ":"))
            os.replace(tmp, self.cache_file)
        except OSError as e:
            log.warning("could not save scan cache: %s", e)

    def load(self):
        try:
            with gzip.open(self.cache_file, "rt") as f:
                payload = json.load(f)
            if payload.get("root") != self.display_root:
                return False
            tree = from_plain(payload["tree"], None)
            with self.lock:
                self.tree, self.info = tree, payload["info"]
            log.info("loaded cached storage scan (%d files)", tree.nfiles)
            return True
        except FileNotFoundError:
            return False
        except Exception as e:
            log.warning("could not load scan cache: %s", e)
            return False

    # -- views
    def status(self):
        with self.lock:
            prog = self.progress
            return {
                "root": self.display_root,
                "ready": self.tree is not None,
                "info": self.info,
                "error": self.error,
                "scanning": None if prog is None else {
                    "files": prog.files, "dirs": prog.dirs, "bytes": prog.bytes, "errors": prog.errors,
                    "current": prog.current.replace(self.host_root, self.display_root, 1),
                    "elapsed": time.time() - prog.started,
                },
            }

    def node_view(self, display_path, max_children=400, top_files=25, top_exts=12, walk=True):
        display_path = self.normalize(display_path)
        with self.lock:
            node = self.find(display_path)
            if node is None:
                return None
            children = []
            for d in node.dirs.values():
                children.append({"name": d.name, "type": "dir", "size": d.size, "files": d.nfiles,
                                 "dirs": d.ndirs, "kind": dominant(d.kinds), "kinds": list(d.kinds),
                                 "mtime": d.mtime, "error": d.error})
            for name, size, mtime, kind in node.files:
                children.append({"name": name, "type": "file", "size": size, "kind": KINDS[kind], "mtime": mtime})
            children.sort(key=lambda c: c["size"], reverse=True)
            if len(children) > max_children:
                rest = children[max_children:]
                children = children[:max_children]
                kinds = [0] * len(KINDS)
                for c in rest:
                    if c["type"] == "dir":
                        for i, v in enumerate(c["kinds"]):
                            kinds[i] += v
                    else:
                        kinds[KINDS.index(c["kind"])] += c["size"]
                children.append({"name": f"{len(rest)} smaller items", "type": "group",
                                 "size": sum(c["size"] for c in rest), "kind": dominant(kinds), "count": len(rest)})

            # walk the subtree once for file-type and largest-file breakdowns
            exts, largest = {}, []
            stack = [(node, display_path)] if walk else []
            while stack:
                d, p = stack.pop()
                for name, size, mtime, kind in d.files:
                    e = ext_of(name) or "(none)"
                    agg = exts.get(e)
                    if agg is None:
                        exts[e] = [size, 1, kind]
                    else:
                        agg[0] += size
                        agg[1] += 1
                    if len(largest) < top_files:
                        heapq.heappush(largest, (size, p + "/" + name, KINDS[kind], mtime))
                    elif size > largest[0][0]:
                        heapq.heapreplace(largest, (size, p + "/" + name, KINDS[kind], mtime))
                for c in d.dirs.values():
                    stack.append((c, p + "/" + c.name))
            ext_list = sorted(({"ext": e, "size": v[0], "files": v[1], "kind": KINDS[v[2]]} for e, v in exts.items()),
                              key=lambda x: x["size"], reverse=True)
            return {
                "path": display_path,
                "root": self.display_root,
                "size": node.size, "files": node.nfiles, "dirs": node.ndirs,
                "kinds": dict(zip(KINDS, node.kinds)), "error": node.error,
                "children": children,
                "exts": ext_list[:top_exts],
                "exts_other": {"size": sum(x["size"] for x in ext_list[top_exts:]),
                               "count": max(0, len(ext_list) - top_exts)},
                "largest": [{"path": p, "size": s, "kind": k, "mtime": m}
                            for s, p, k, m in sorted(largest, reverse=True)],
            }
