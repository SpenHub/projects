#!/usr/bin/env python3
"""girthy-dash: a small, dependency-free monitoring dashboard for an Unraid host.

Background collectors:
  fast   every FAST_INTERVAL s  CPU / RAM / swap (Unraid API), NIC throughput,
                                /transcode ramdisk + docker.img usage, CPU clocks
  slow   every SLOW_INTERVAL s  array / cache disks, temperatures, container states
  sizes  every SIZE_INTERVAL s  per-container disk usage (a slow API call)
  gpu    continuous            `intel_gpu_top -J` stream (needs CAP_PERFMON + /dev/dri)
  net    every PING_INTERVAL s (default 300)  ping the gateway + internet targets; persisted so
                                uptime history survives restarts

HTTP (stdlib ThreadingHTTPServer):
  GET /                      the dashboard (static/index.html)
  GET /api/now               current snapshot of everything
  GET /api/history?range=R   time series for R in 5m, 15m, 1h, 6h, 24h, 7d
  GET /storage               the storage page (static/storage.html)
  GET /api/storage/status    scan status
  GET /api/storage/node?path=P   one folder: children, file types, largest files, *arr tags
  GET /api/storage/media     /data grouped into movies, shows, ... with *arr tags (media map)
  GET /api/docker            containers with live stats, health, uptime history, recent events
  POST /api/docker/NAME/restart   stop + start a container (guarded; needs DOCKER_KEY_FILE)
  GET /api/arr/library       Radarr movies + Sonarr series with sizes
  GET /api/whoami            whether this client may make changes
  POST /api/storage/scan     start a full rescan                       (guarded)
  POST /api/arr/radarr/movie/ID/delete    {mode: files|remove, exclusion}   (guarded)
  POST /api/arr/sonarr/series/ID/delete   {mode: files|remove, exclusion, season?} (guarded)
  GET /healthz               liveness
"""

import collections
import concurrent.futures
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import arr as arrmod
import auth
import docker_mon
import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("girthy-dash")


def env(name, default):
    return os.environ.get(name, default)


PORT = int(env("PORT", "7879"))
API_URL = env("UNRAID_API_URL", "http://127.0.0.1/graphql")
API_KEY_FILE = env("UNRAID_API_KEY_FILE", "/config/api_key")
NET_IFACE = env("NET_IFACE", "br0")
INET_TARGETS = [t.strip() for t in env("PING_TARGETS", "1.1.1.1,8.8.8.8").split(",") if t.strip()]
GATEWAY = env("PING_GATEWAY", "")
TRANSCODE_PATH = env("TRANSCODE_PATH", "/host/transcode")
DOCKER_PATH = env("DOCKER_PATH", "/host/docker")
DATA_DIR = env("DATA_DIR", "/data")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SCAN_HOST_ROOT = env("SCAN_HOST_ROOT", "/host/user")
SCAN_DISPLAY_ROOT = env("SCAN_DISPLAY_ROOT", "/mnt/user")
SCAN_HOUR = int(env("SCAN_HOUR", "5"))          # daily full rescan at this local hour; -1 disables
ARR_CONFIG = env("ARR_CONFIG", "/config/arr.json")
TRUSTED_NETS = env("TRUSTED_NETS", "192.168.0.0/16,10.0.0.0/8,100.64.0.0/10,127.0.0.0/8")
CF_ACCESS_TEAM = env("CF_ACCESS_TEAM", "shy-dream-5b6f")
CF_ACCESS_AUD = env("CF_ACCESS_AUD", "")
DOCKER_KEY_FILE = env("DOCKER_KEY_FILE", "/config/docker_key")   # optional Unraid key allowed to start/stop containers
SELF_CONTAINER = env("SELF_CONTAINER", "girthy-dash")

FAST_INTERVAL = 3
SLOW_INTERVAL = 15
SIZE_INTERVAL = 300
PING_INTERVAL = int(env("PING_INTERVAL", "300"))   # seconds between network checks
PING_RETRIES = 2             # re-check a failure this many times (5 s apart) before recording it
GPU_INTERVAL_MS = 2000
RING_SECONDS = 3600          # in-memory high-resolution history (5m / 15m / 1h views)
METRIC_KEEP_DAYS = 183       # per-minute rollups (6h / 24h / 7d views), ~6 months
CHECK_KEEP_DAYS = 183        # network checks (uptime %), ~6 months
EVENT_KEEP_DAYS = 183        # container state / health / restart history
MAX_POINTS = 360             # max points per series sent to the browser

METRIC_KEYS = ["cpu", "mem", "swap", "rx", "tx", "transcode",
               "gpu_video", "gpu_render", "gpu_enhance", "gpu_blitter"]
RANGES = {"5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}

# --------------------------------------------------------------------------- state

lock = threading.Lock()
state = {
    "host": {},
    "cpu": None,
    "memory": None,
    "gpu": None,
    "temps": None,
    "storage": None,
    "containers": None,
    "transcode": None,
    "docker_img": None,
    "nic": None,
    "net": None,
    "errors": {},
    "started": time.time(),
}
container_sizes = {}
ring = collections.deque()   # (ts, {metric: value})


def set_error(name, err):
    with lock:
        if err is None:
            state["errors"].pop(name, None)
        else:
            state["errors"][name] = str(err)[:300]
            log.warning("%s: %s", name, err)


# --------------------------------------------------------------------------- sqlite

db_lock = threading.Lock()
db = None


def db_init():
    global db
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(os.path.join(DATA_DIR, "dash.db"), check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    cols = ", ".join(f"{k} REAL" for k in METRIC_KEYS)
    db.execute(f"CREATE TABLE IF NOT EXISTS metrics (ts INTEGER PRIMARY KEY, {cols})")
    db.execute("CREATE TABLE IF NOT EXISTS checks (ts INTEGER PRIMARY KEY, gw_ok INTEGER, "
               "gw_ms REAL, inet_ok INTEGER, inet_ms REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS cevents (ts REAL, name TEXT, kind TEXT, state TEXT, health TEXT, detail TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS cevents_name_ts ON cevents (name, ts)")
    db.execute("CREATE INDEX IF NOT EXISTS cevents_ts ON cevents (ts)")
    db.commit()


def db_query(sql, args=()):
    with db_lock:
        return db.execute(sql, args).fetchall()


def db_write(sql, args=()):
    with db_lock:
        db.execute(sql, args)
        db.commit()


def db_prune():
    now = time.time()
    with db_lock:
        db.execute("DELETE FROM metrics WHERE ts < ?", (now - METRIC_KEEP_DAYS * 86400,))
        db.execute("DELETE FROM checks WHERE ts < ?", (now - CHECK_KEEP_DAYS * 86400,))
        db.execute("DELETE FROM cevents WHERE ts < ? AND kind != 'seen'", (now - EVENT_KEEP_DAYS * 86400,))
        db.commit()


class MinuteRollup:
    """Averages fast samples into one row per minute in the metrics table."""

    def __init__(self):
        self.minute = None
        self.sums, self.counts = {}, {}

    def add(self, ts, sample):
        minute = int(ts // 60) * 60
        if self.minute is not None and minute != self.minute:
            self.flush()
        self.minute = minute
        for k, v in sample.items():
            if v is not None:
                self.sums[k] = self.sums.get(k, 0.0) + v
                self.counts[k] = self.counts.get(k, 0) + 1

    def flush(self):
        if self.minute is None or not self.counts:
            return
        row = [self.sums[k] / self.counts[k] if self.counts.get(k) else None for k in METRIC_KEYS]
        cols = ", ".join(METRIC_KEYS)
        marks = ", ".join("?" for _ in METRIC_KEYS)
        try:
            db_write(f"INSERT OR REPLACE INTO metrics (ts, {cols}) VALUES (?, {marks})",
                     [self.minute] + row)
        except sqlite3.Error as e:
            set_error("database", e)
        self.sums, self.counts = {}, {}


# --------------------------------------------------------------------------- unraid api

API_KEY = ""


def gql(query, timeout=20, key=None):
    req = urllib.request.Request(
        API_URL, data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json", "x-api-key": key or API_KEY})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise RuntimeError("; ".join(e.get("message", "?") for e in body["errors"]))
    return body["data"]


# --------------------------------------------------------------------------- host helpers

def fs_usage(path):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return {"total": total, "used": used, "free": free,
            "pct": used / total * 100 if total else 0.0}


def read_nic_bytes(iface):
    with open("/proc/net/dev") as f:
        for line in f:
            name, _, rest = line.partition(":")
            if name.strip() == iface:
                fields = rest.split()
                return int(fields[0]), int(fields[8])
    raise RuntimeError(f"interface {iface} not found in /proc/net/dev")


def read_cpu_clocks():
    mhz, model = [], None
    with open("/proc/cpuinfo") as f:
        for line in f:
            if line.startswith("cpu MHz"):
                mhz.append(float(line.split(":")[1]))
            elif model is None and line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
    return model, mhz


def default_gateway():
    with open("/proc/net/route") as f:
        for line in f.readlines()[1:]:
            parts = line.split()
            if len(parts) > 2 and parts[1] == "00000000" and parts[2] != "00000000":
                g = int(parts[2], 16)
                return ".".join(str((g >> s) & 0xFF) for s in (0, 8, 16, 24))
    return None


def read_gpu_max_freq():
    try:
        for card in sorted(os.listdir("/sys/class/drm")):
            p = f"/sys/class/drm/{card}/gt_max_freq_mhz"
            if re.fullmatch(r"card\d+", card) and os.path.exists(p):
                with open(p) as f:
                    return int(f.read().strip())
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------- collectors

def fast_loop():
    rollup = MinuteRollup()
    prev_nic = None
    last_prune = 0
    while True:
        t0 = time.time()
        sample = {}

        try:
            d = gql("{ metrics { cpu { percentTotal cpus { percentTotal } } "
                    "memory { total available used buffcache swapTotal swapUsed } } }")
            cpu, mem = d["metrics"]["cpu"], d["metrics"]["memory"]
            model, clocks = read_cpu_clocks()
            total, avail = int(mem["total"]), int(mem["available"])
            swap_total, swap_used = int(mem["swapTotal"] or 0), int(mem["swapUsed"] or 0)
            with lock:
                state["cpu"] = {
                    "pct": cpu["percentTotal"],
                    "threads": [c["percentTotal"] for c in cpu["cpus"]],
                    "mhz_avg": sum(clocks) / len(clocks) if clocks else None,
                    "mhz_max": max(clocks) if clocks else None,
                    "load": os.getloadavg(),
                }
                state["memory"] = {
                    "total": total, "available": avail, "used": total - avail,
                    "cache": int(mem["buffcache"] or 0),
                    "pct": (total - avail) / total * 100 if total else 0.0,
                    "swap_total": swap_total, "swap_used": swap_used,
                    "swap_pct": swap_used / swap_total * 100 if swap_total else None,
                }
                state["host"]["cpu_model"] = model
            sample["cpu"] = cpu["percentTotal"]
            sample["mem"] = state["memory"]["pct"]
            sample["swap"] = state["memory"]["swap_pct"]
            set_error("api", None)
        except Exception as e:
            set_error("api", f"Unraid API: {e}")

        for key, path, name in (("transcode", TRANSCODE_PATH, "/transcode"),
                                ("docker_img", DOCKER_PATH, "docker.img")):
            try:
                usage = fs_usage(path)
                with lock:
                    state[key] = usage
                if key == "transcode":
                    sample["transcode"] = usage["pct"]
                set_error(key, None)
            except OSError as e:
                set_error(key, f"{name}: {e}")

        try:
            rx, tx = read_nic_bytes(NET_IFACE)
            now = time.time()
            if prev_nic:
                dt = now - prev_nic[0]
                rx_bps = max(0, rx - prev_nic[1]) * 8 / dt
                tx_bps = max(0, tx - prev_nic[2]) * 8 / dt
                with lock:
                    state["nic"] = {"iface": NET_IFACE, "rx_bps": rx_bps, "tx_bps": tx_bps,
                                    "rx_total": rx, "tx_total": tx}
                sample["rx"], sample["tx"] = rx_bps, tx_bps
            prev_nic = (now, rx, tx)
            set_error("nic", None)
        except Exception as e:
            set_error("nic", e)

        try:
            with open("/proc/uptime") as f:
                up = float(f.read().split()[0])
            with lock:
                state["host"]["uptime"] = up
        except OSError:
            pass

        with lock:
            gpu = state["gpu"]
            if gpu and not gpu.get("error") and time.time() - gpu.get("ts", 0) < 10:
                for eng in ("video", "render", "enhance", "blitter"):
                    sample[f"gpu_{eng}"] = gpu["engines"].get(eng)

        ts = time.time()
        with lock:
            ring.append((ts, sample))
            while ring and ring[0][0] < ts - RING_SECONDS:
                ring.popleft()
        rollup.add(ts, sample)

        if ts - last_prune > 3600:
            last_prune = ts
            try:
                db_prune()
            except sqlite3.Error as e:
                set_error("database", e)

        time.sleep(max(0.5, FAST_INTERVAL - (time.time() - t0)))


def pick_temps(sensors):
    out = {"cpu": None, "cores": [], "pch": None, "board": None, "nvme": [], "fans": []}
    for s in sensors:
        name, typ = s["name"], s["type"]
        val = (s.get("current") or {}).get("value")
        if val is None:
            continue
        if typ == "CPU_CORE":
            if name.endswith("CPU Temp") or "Package" in name:
                out["cpu"] = val
            elif "Core" in name:
                out["cores"].append(val)
        elif "pch" in name.lower() and name.endswith("temp1"):
            out["pch"] = val
        elif name.endswith("MB Temp") or name.endswith("SYSTIN"):
            out["board"] = out["board"] if out["board"] is not None else val
        elif typ == "NVME" and "Composite" not in name and "Sensor" not in name:
            out["nvme"].append({"name": name, "value": val})
        elif "fan" in name.lower() and val > 0:
            out["fans"].append({"name": name.split(" ", 1)[-1], "rpm": val})
    if out["cpu"] is None and out["cores"]:
        out["cpu"] = max(out["cores"])
    return out


def disk_entry(d, kind):
    kb = 1024
    size = d.get("fsSize")
    used = d.get("fsUsed")
    return {
        "name": d["name"], "kind": kind,
        "size": int(size) * kb if size else (int(d["size"]) * kb if d.get("size") else None),
        "used": int(used) * kb if used is not None else None,
        "free": int(d["fsFree"]) * kb if d.get("fsFree") is not None else None,
        "temp": d.get("temp"), "status": d.get("status"),
        "spinning": d.get("isSpinning"), "rotational": d.get("rotational"),
        "errors": int(d["numErrors"]) if d.get("numErrors") is not None else None,
    }


def slow_loop():
    while True:
        t0 = time.time()
        try:
            d = gql("{ array { state capacity { kilobytes { total used free } } "
                    "disks { name size fsSize fsUsed fsFree temp status isSpinning rotational numErrors } "
                    "parities { name size temp status isSpinning rotational numErrors } "
                    "caches { name size fsSize fsUsed fsFree temp status isSpinning rotational numErrors } } "
                    "docker { containers { id names image state status autoStart autoStartOrder created webUiUrl "
                    "isUpdateAvailable hostConfig { networkMode } ports { ip privatePort publicPort type } } } }")
            arr = d["array"]
            cap = (arr.get("capacity") or {}).get("kilobytes") or {}
            disks = ([disk_entry(x, "parity") for x in arr.get("parities") or []]
                     + [disk_entry(x, "data") for x in arr.get("disks") or []]
                     + [disk_entry(x, "cache") for x in arr.get("caches") or []])
            containers = []
            for c in d["docker"]["containers"]:
                name = (c.get("names") or ["?"])[0].lstrip("/")
                containers.append({
                    "id": c.get("id"), "name": name, "image": c.get("image"), "state": c.get("state"),
                    "status": c.get("status"), "autostart": c.get("autoStart"),
                    "autostart_order": c.get("autoStartOrder"), "created": c.get("created"),
                    "webui": c.get("webUiUrl"), "update": c.get("isUpdateAvailable"),
                    "network": (c.get("hostConfig") or {}).get("networkMode"),
                    "ports": sorted({(p.get("publicPort"), p.get("privatePort"), (p.get("type") or "").lower())
                                     for p in c.get("ports") or [] if p.get("publicPort")}),
                })
            with lock:
                state["storage"] = {
                    "array_state": arr.get("state"),
                    "total": int(cap["total"]) * 1024 if cap.get("total") else None,
                    "used": int(cap["used"]) * 1024 if cap.get("used") else None,
                    "free": int(cap["free"]) * 1024 if cap.get("free") else None,
                    "disks": disks,
                }
                state["containers"] = containers
            set_error("storage", None)
            try:
                tracker.observe(containers, time.time())
            except Exception as e:
                log.warning("container history: %s", e)
        except Exception as e:
            set_error("storage", f"Unraid API (array/docker): {e}")

        try:
            d = gql("{ metrics { temperature { sensors { name type current { value } } } } }", timeout=30)
            temps = pick_temps(d["metrics"]["temperature"]["sensors"])
            with lock:
                state["temps"] = temps
            set_error("temps", None)
        except Exception as e:
            set_error("temps", f"Unraid API (temperatures): {e}")

        time.sleep(max(1, SLOW_INTERVAL - (time.time() - t0)))


def sizes_loop():
    while True:
        try:
            d = gql("{ docker { containers { names sizeRootFs sizeRw sizeLog } } }", timeout=60)
            sizes = {}
            for c in d["docker"]["containers"]:
                name = (c.get("names") or ["?"])[0].lstrip("/")
                sizes[name] = {k: (int(c[k]) if c.get(k) is not None else None)
                               for k in ("sizeRootFs", "sizeRw", "sizeLog")}
            with lock:
                container_sizes.clear()
                container_sizes.update(sizes)
            set_error("sizes", None)
        except Exception as e:
            set_error("sizes", f"Unraid API (container sizes): {e}")
        time.sleep(SIZE_INTERVAL)


ENGINE_KEYS = {"Render/3D": "render", "Video": "video", "VideoEnhance": "enhance", "Blitter": "blitter"}


def handle_gpu_sample(raw, max_freq):
    s = json.loads(raw)
    period = (s.get("period") or {}).get("duration", 0)
    if period < 500:   # the first sample covers only a few ms; skip it
        return
    engines = {}
    for name, e in (s.get("engines") or {}).items():
        base = re.sub(r"/\d+$", "", name)
        key = ENGINE_KEYS.get(base)
        if key:
            engines[key] = max(engines.get(key, 0.0), float(e.get("busy", 0.0)))
    freq = s.get("frequency") or {}
    power = s.get("power") or {}
    with lock:
        state["gpu"] = {
            "ts": time.time(),
            "engines": engines,
            "freq": freq.get("actual"),
            "freq_req": freq.get("requested"),
            "freq_max": max_freq,
            "power": power.get("GPU"),
            "package_power": power.get("Package"),
            "rc6": (s.get("rc6") or {}).get("value"),
        }
    set_error("gpu", None)


def gpu_loop():
    max_freq = read_gpu_max_freq()
    cmd = ["stdbuf", "-o0", "intel_gpu_top", "-J", "-s", str(GPU_INTERVAL_MS)]
    while True:
        try:
            with open("/tmp/intel_gpu_top.err", "w+") as errf:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True)
                depth, buf = 0, []
                while True:
                    ch = proc.stdout.read(1)
                    if not ch:
                        break
                    if ch == "{":
                        depth += 1
                    if depth:
                        buf.append(ch)
                    if ch == "}" and depth:
                        depth -= 1
                        if depth == 0:
                            try:
                                handle_gpu_sample("".join(buf), max_freq)
                            except (ValueError, TypeError) as e:
                                log.warning("bad intel_gpu_top sample: %s", e)
                            buf = []
                proc.wait()
                errf.seek(0)
                err = errf.read().strip().splitlines()
            msg = f"intel_gpu_top exited with code {proc.returncode}"
            if err:
                msg += f": {err[-1]}"
            with lock:
                state["gpu"] = {"error": msg}
            set_error("gpu", msg)
        except FileNotFoundError:
            with lock:
                state["gpu"] = {"error": "intel_gpu_top is not installed"}
            set_error("gpu", "intel_gpu_top is not installed")
        except Exception as e:
            with lock:
                state["gpu"] = {"error": str(e)}
            set_error("gpu", e)
        time.sleep(15)


def ping(host):
    try:
        r = subprocess.run(["ping", "-n", "-c", "1", "-W", "2", host],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            m = re.search(r"time[=<]([\d.]+)", r.stdout)
            return True, float(m.group(1)) if m else None
    except Exception:
        pass
    return False, None


def uptime_block(now):
    """Uptime %, current streak, outages and status strips, from the checks table."""
    out = {}
    for label, secs in (("24h", 86400), ("7d", 604800), ("30d", 2592000)):
        n, ok = db_query("SELECT COUNT(*), COALESCE(SUM(inet_ok), 0) FROM checks WHERE ts >= ?",
                         (now - secs,))[0]
        out[f"pct_{label}"] = ok / n * 100 if n else None
        out[f"coverage_{label}"] = min(1.0, n * PING_INTERVAL / secs)

    last = db_query("SELECT inet_ok FROM checks ORDER BY ts DESC LIMIT 1")
    if last:
        cur = last[0][0]
        flip = db_query("SELECT MAX(ts) FROM checks WHERE inet_ok != ?", (cur,))[0][0]
        if flip is None:
            since = db_query("SELECT MIN(ts) FROM checks")[0][0]
        else:
            since = db_query("SELECT MIN(ts) FROM checks WHERE ts > ?", (flip,))[0][0]
        out["since"] = since
        out["since_is_start"] = flip is None

    # outages: runs of failed internet checks in the last 30 days
    fails = db_query("SELECT ts FROM checks WHERE inet_ok = 0 AND ts >= ? ORDER BY ts",
                     (now - 2592000,))
    outages, start, prev = [], None, None
    for (ts,) in fails:
        if start is None:
            start = prev = ts
        elif ts - prev > PING_INTERVAL * 2.5:
            outages.append([start, prev + PING_INTERVAL])
            start = prev = ts
        else:
            prev = ts
    if start is not None:
        outages.append([start, prev + PING_INTERVAL])
    out["outages"] = [{"start": s, "end": e, "duration": e - s} for s, e in reversed(outages[-20:])]

    def strip(window, buckets):
        width = window / buckets
        start_ts = now - window
        rows = db_query("SELECT CAST((ts - ?) / ? AS INTEGER) AS b, COUNT(*), SUM(inet_ok) "
                        "FROM checks WHERE ts >= ? GROUP BY b", (start_ts, width, start_ts))
        by_b = {b: (n, ok) for b, n, ok in rows}
        cells = []
        for b in range(buckets):
            n, ok = by_b.get(b, (0, 0))
            cells.append({"start": start_ts + b * width, "end": start_ts + (b + 1) * width,
                          "checks": n, "ok": ok or 0})
        return cells

    out["strip_24h"] = strip(86400, 96)
    out["strip_30d"] = strip(2592000, 30)
    return out


def net_loop():
    gateway = GATEWAY or default_gateway()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1 + len(INET_TARGETS))
    while True:
        t0 = time.time()
        try:
            futs = {h: pool.submit(ping, h) for h in ([gateway] if gateway else []) + INET_TARGETS}
            res = {h: f.result() for h, f in futs.items()}
            # with checks minutes apart, one dropped ping shouldn't count as a long outage
            for _ in range(PING_RETRIES):
                if any(res[h][0] for h in INET_TARGETS) and (not gateway or res[gateway][0]):
                    break
                time.sleep(5)
                retry = {h: pool.submit(ping, h) for h, (ok, _) in res.items() if not ok}
                for h, f in retry.items():
                    if f.result()[0]:
                        res[h] = f.result()
            gw_ok, gw_ms = res.get(gateway, (None, None)) if gateway else (None, None)
            inet = [res[h] for h in INET_TARGETS]
            inet_ok = any(ok for ok, _ in inet)
            lat = [ms for ok, ms in inet if ok and ms is not None]
            inet_ms = min(lat) if lat else None
            ts = int(t0)
            db_write("INSERT OR REPLACE INTO checks (ts, gw_ok, gw_ms, inet_ok, inet_ms) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (ts, None if gw_ok is None else int(gw_ok), gw_ms, int(inet_ok), inet_ms))
            block = uptime_block(time.time())
            with lock:
                state["net"] = {
                    "ts": ts, "gateway": gateway, "gw_ok": gw_ok, "gw_ms": gw_ms,
                    "inet_ok": inet_ok, "inet_ms": inet_ms,
                    "targets": [{"host": h, "ok": res[h][0], "ms": res[h][1]} for h in INET_TARGETS],
                    "interval": PING_INTERVAL,
                    **block,
                }
            set_error("net", None)
        except Exception as e:
            set_error("net", e)
        time.sleep(max(1, PING_INTERVAL - (time.time() - t0)))


# --------------------------------------------------------------------------- history

def downsample(points, keys, start, end):
    """points: [(ts, {key: value})] sorted. Averages into <= MAX_POINTS buckets."""
    width = max((end - start) / MAX_POINTS, 1e-9)
    buckets = collections.OrderedDict()
    for ts, d in points:
        if ts < start:
            continue
        b = int((ts - start) / width)
        acc = buckets.setdefault(b, ({}, {}, []))
        acc[2].append(ts)
        for k in keys:
            v = d.get(k)
            if v is not None:
                acc[0][k] = acc[0].get(k, 0.0) + v
                acc[1][k] = acc[1].get(k, 0) + 1
    t, series = [], {k: [] for k in keys}
    for b, (sums, counts, stamps) in buckets.items():
        t.append(sum(stamps) / len(stamps))
        for k in keys:
            series[k].append(round(sums[k] / counts[k], 3) if counts.get(k) else None)
    return t, series


def history(rng):
    secs = RANGES[rng]
    now = time.time()
    start = now - secs
    cols = ", ".join(METRIC_KEYS)

    def minute_rows(until):
        rows = db_query(f"SELECT ts, {cols} FROM metrics WHERE ts >= ? AND ts + 60 <= ? ORDER BY ts",
                        (start - 60, until))
        return [(r[0] + 30, dict(zip(METRIC_KEYS, r[1:]))) for r in rows]

    if secs <= RING_SECONDS:
        with lock:
            points = [p for p in ring if p[0] >= start]
        # after a restart the ring is short: backfill the older part from minute rollups
        first = points[0][0] if points else now
        if first - start > 120:
            points = minute_rows(first) + points
        step = 60 if points and points[0][0] < first else FAST_INTERVAL
    else:
        points = minute_rows(now + 60)
        step = 60
    t, series = downsample(points, METRIC_KEYS, start, now)

    rows = db_query("SELECT ts, gw_ms, inet_ms, inet_ok FROM checks WHERE ts >= ? ORDER BY ts", (start,))
    lt, lat = downsample([(r[0], {"gw": r[1], "inet": r[2], "inet_ok": r[3]}) for r in rows],
                         ["gw", "inet", "inet_ok"], start, now)
    width = secs / MAX_POINTS
    return {
        "range": rng, "start": start, "end": now,
        "step": max(step, width), "t": t, "series": series,
        "latency": {"t": lt, "step": max(PING_INTERVAL, width), **lat},
    }


# --------------------------------------------------------------------------- storage / arr

stats = docker_mon.StatsStream(API_URL, lambda: API_KEY)
tracker = None
scanner = storage.Scanner(SCAN_HOST_ROOT, SCAN_DISPLAY_ROOT, DATA_DIR)
arr = arrmod.Arr(ARR_CONFIG)
guard = auth.Guard(TRUSTED_NETS, CF_ACCESS_TEAM, CF_ACCESS_AUD)


def storage_loop():
    if not scanner.load() and os.path.isdir(SCAN_HOST_ROOT):
        scanner.start()
    last_auto = None
    while True:
        lt = time.localtime()
        if SCAN_HOUR >= 0 and lt.tm_hour == SCAN_HOUR and last_auto != lt.tm_yday:
            last_auto = lt.tm_yday
            scanner.start()
        time.sleep(60)


def node_payload(path):
    view = scanner.node_view(path)
    if view is None:
        return None
    try:
        own, tags = arr.match(view["path"], view["children"])
        view["arr"] = own
        for c in view["children"]:
            if c["name"] in tags and c["type"] == "dir":
                c["arr"] = tags[c["name"]]
    except Exception as e:
        view["arr_error"] = str(e)
    return view


MEDIA_ROOT = env("MEDIA_ROOT", "/mnt/user/data")
MEDIA_EXPAND = {"media"}          # folders under MEDIA_ROOT whose subfolders become their own groups
GROUP_ORDER = {"movies": 0, "shows": 1, "tv": 1}


def media_payload(max_items=500):
    """/data as groups (movies, shows, music, ISOs, ...) of items, with *arr tags, for the media map."""
    top = scanner.node_view(MEDIA_ROOT, max_children=10000, walk=False)
    if top is None:
        return None
    groups = []
    for c in top["children"]:
        if c["type"] == "dir" and c["name"] in MEDIA_EXPAND:
            base = MEDIA_ROOT + "/" + c["name"]
            sub = scanner.node_view(base, max_children=10000, walk=False) or {"children": []}
            for g in sub["children"]:
                groups.append((base + "/" + g["name"], g) if g["type"] == "dir" else (base, g))
        else:
            groups.append((MEDIA_ROOT + "/" + c["name"], c) if c["type"] == "dir" else (MEDIA_ROOT, c))
    out, loose = [], []
    for path, g in groups:
        if g["type"] != "dir":
            loose.append(g)
            continue
        view = scanner.node_view(path, max_children=max_items, walk=False)
        if view is None or not view["size"]:
            continue
        items = [x for x in view["children"] if x["size"] > 0]
        try:
            _, tags = arr.match(path, items)
        except Exception:
            tags = {}
        for x in items:
            x.pop("kinds", None)
            if x["type"] == "dir" and x["name"] in tags:
                x["arr"] = tags[x["name"]]
        out.append({"name": g["name"], "path": path, "size": view["size"], "files": view["files"], "items": items})
    if loose:
        out.append({"name": "loose files", "path": MEDIA_ROOT, "size": sum(x["size"] for x in loose),
                    "files": len(loose), "items": loose})
    out.sort(key=lambda g: (GROUP_ORDER.get(g["name"].lower(), 2), -g["size"]))
    return {"path": MEDIA_ROOT, "size": top["size"], "files": top["files"], "groups": out}


# --------------------------------------------------------------------------- docker page

def docker_key():
    try:
        with open(DOCKER_KEY_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def docker_payload():
    now = time.time()
    snap = snapshot()
    out = []
    for c in snap.get("containers") or []:
        st, spark = stats.get(docker_mon.short_id(c.get("id")))
        c = dict(c)
        c["health"] = docker_mon.health_of(c.get("status"))
        c["stats"] = st
        c["spark"] = [[t, cpu, mem] for t, cpu, mem in spark]
        c["uptime"] = tracker.uptime(c["name"], now, c.get("status"))
        c["events"] = tracker.events(now, limit=12, name=c["name"])
        c["is_self"] = c["name"] == SELF_CONTAINER
        out.append(c)
    gone = [n for n, p in tracker.prev.items() if p["state"] == "GONE"]
    return {
        "now": now, "containers": out, "docker_img": snap.get("docker_img"),
        "cores": len((snap.get("cpu") or {}).get("threads") or []) or None,
        "mem_total": (snap.get("memory") or {}).get("total"),
        "events": tracker.events(now, limit=60), "removed": gone,
        "stream": stats.status(), "can_restart": docker_key() is not None,
        "errors": snap.get("errors", {}),
    }


def restart_container(name, who):
    key = docker_key()
    if not key:
        raise PermissionError("Restarts need a Docker control key on the server (not set up yet).")
    if name == SELF_CONTAINER:
        raise PermissionError("The dashboard can't restart itself. Restart it from the Unraid Docker tab.")
    with lock:
        c = next((x for x in state.get("containers") or [] if x["name"] == name), None)
    if not c:
        raise LookupError(f"no container named {name}")
    cid = json.dumps(c["id"])
    if (c.get("state") or "").upper() == "RUNNING":
        gql(f"mutation {{ docker {{ stop(id: {cid}) {{ id state }} }} }}", timeout=120, key=key)
    gql(f"mutation {{ docker {{ start(id: {cid}) {{ id state }} }} }}", timeout=120, key=key)
    tracker.note_manual_restart(name, who)
    log.info("restarted container %s (requested by %s)", name, who)
    return f"Restarted {name}."


def run_arr_action(service, item_id, body):
    mode = body.get("mode")
    exclusion = bool(body.get("exclusion"))
    if service == "radarr":
        path, msg = arr.delete_movie(item_id, mode, exclusion)
    else:
        season = body.get("season")
        path, msg = arr.delete_series(item_id, mode, exclusion, None if season is None else int(season))
    try:
        if path and (path == SCAN_DISPLAY_ROOT or path.startswith(SCAN_DISPLAY_ROOT + "/")):
            scanner.rescan_path(path)
    except Exception as e:
        log.warning("rescan after delete failed: %s", e)
    return msg


# --------------------------------------------------------------------------- http

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/storage": ("storage.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/common.js": ("common.js", "text/javascript; charset=utf-8"),
    "/storage.js": ("storage.js", "text/javascript; charset=utf-8"),
    "/docker": ("docker.html", "text/html; charset=utf-8"),
    "/docker.js": ("docker.js", "text/javascript; charset=utf-8"),
}
RESTART_RE = re.compile(r"/api/docker/([A-Za-z0-9_.-]{1,128})/restart")
ACTION_RE = re.compile(r"/api/arr/(radarr)/movie/(\d+)/delete|/api/arr/(sonarr)/series/(\d+)/delete")

def snapshot():
    with lock:
        snap = json.loads(json.dumps(state))
        sizes = dict(container_sizes)
    for c in snap.get("containers") or []:
        c.update(sizes.get(c["name"], {}))
    snap["now"] = time.time()
    return snap


FONT_RE = re.compile(r"/fonts/[a-z0-9-]+\.woff2")


class Handler(BaseHTTPRequestHandler):
    server_version = "girthy-dash"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send(code, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        try:
            q = urllib.parse.parse_qs(url.query)
            if url.path in STATIC_FILES:
                name, ctype = STATIC_FILES[url.path]
                with open(os.path.join(STATIC_DIR, name), "rb") as f:
                    self.send(200, f.read(), ctype, "no-cache")
            elif url.path == "/api/storage/status":
                self.send_json(scanner.status())
            elif url.path == "/api/storage/node":
                try:
                    view = node_payload(q.get("path", [SCAN_DISPLAY_ROOT])[0])
                except ValueError as e:
                    self.send_json({"error": str(e)}, 400)
                    return
                if view is None:
                    self.send_json({"error": "not found (still scanning, or the folder no longer exists)"}, 404)
                else:
                    self.send_json(view)
            elif url.path == "/api/storage/media":
                view = media_payload()
                if view is None:
                    self.send_json({"error": f"{MEDIA_ROOT} isn't in the scan yet"}, 404)
                else:
                    self.send_json(view)
            elif url.path == "/api/docker":
                self.send_json(docker_payload())
            elif url.path == "/api/arr/library":
                self.send_json(arr.library(force=q.get("refresh") == ["1"]))
            elif url.path == "/api/whoami":
                ok, who = guard.check(self, require_header=False)
                self.send_json({"can_write": ok, "who": who if ok else None, "reason": None if ok else who})
            elif url.path == "/api/now":
                self.send_json(snapshot())
            elif url.path == "/api/history":
                rng = q.get("range", ["1h"])[0]
                if rng not in RANGES:
                    self.send_json({"error": f"range must be one of {list(RANGES)}"}, 400)
                else:
                    self.send_json(history(rng))
            elif FONT_RE.fullmatch(url.path):
                path = os.path.join(STATIC_DIR, "fonts", os.path.basename(url.path))
                if os.path.isfile(path):
                    with open(path, "rb") as f:
                        self.send(200, f.read(), "font/woff2", "public, max-age=31536000, immutable")
                else:
                    self.send(404, b"not found", "text/plain")
            elif url.path == "/healthz":
                self.send(200, b"ok", "text/plain")
            else:
                self.send(404, b"not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as e:
            log.exception("request failed")
            try:
                self.send_json({"error": str(e)}, 500)
            except Exception:
                pass


def _do_post(self):
    url = urllib.parse.urlparse(self.path)
    ok, who = guard.check(self)
    if not ok:
        self.send_json({"error": who}, 403)
        return
    try:
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if 0 < n <= 65536 else {}
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        self.send_json({"error": "invalid JSON body"}, 400)
        return
    try:
        if url.path == "/api/storage/scan":
            started = scanner.start()
            log.info("storage scan requested by %s (started=%s)", who, started)
            self.send_json({"ok": True, "started": started})
            return
        rm = RESTART_RE.fullmatch(url.path)
        if rm:
            try:
                msg = restart_container(rm.group(1), who)
            except PermissionError as e:
                self.send_json({"error": str(e)}, 403)
                return
            except LookupError as e:
                self.send_json({"error": str(e)}, 404)
                return
            except Exception as e:
                log.warning("restart %s failed: %s", rm.group(1), e)
                self.send_json({"error": f"Restart failed: {e}"}, 502)
                return
            self.send_json({"ok": True, "message": msg})
            return
        m = ACTION_RE.fullmatch(url.path)
        if not m:
            self.send_json({"error": "not found"}, 404)
            return
        service = m.group(1) or m.group(3)
        item_id = m.group(2) or m.group(4)
        if body.get("mode") not in ("files", "remove"):
            self.send_json({"error": "mode must be 'files' or 'remove'"}, 400)
            return
        log.info("%s delete %s/%s %s requested by %s", service, item_id, body.get("season"), body.get("mode"), who)
        msg = run_arr_action(service, item_id, body)
        self.send_json({"ok": True, "message": msg})
    except arrmod.ArrError as e:
        self.send_json({"error": str(e)}, 502)
    except BrokenPipeError:
        pass
    except Exception as e:
        log.exception("POST failed")
        self.send_json({"error": str(e)}, 500)


Handler.do_POST = _do_post


def main():
    global API_KEY
    try:
        with open(API_KEY_FILE) as f:
            API_KEY = f.read().strip()
    except OSError as e:
        set_error("api", f"cannot read API key file {API_KEY_FILE}: {e}")
    db_init()
    global tracker
    tracker = docker_mon.Tracker(db_query, db_write)
    for target in (fast_loop, slow_loop, sizes_loop, gpu_loop, net_loop, storage_loop, stats.run, stats.sampler):
        threading.Thread(target=target, name=target.__name__, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log.info("girthy-dash listening on :%d", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
