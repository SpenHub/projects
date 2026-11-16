"""Docker page data: live per-container stats, state/health history and uptime.

Stats come from the Unraid API's `dockerContainerStats` GraphQL subscription
(graphql-transport-ws over a plain stdlib WebSocket), so no docker socket or
extra host mounts are needed. State history is kept as change events in SQLite.
"""

import base64
import collections
import json
import logging
import os
import re
import socket
import struct
import threading
import time
import urllib.parse

log = logging.getLogger("girthy-dash.docker")

SPARK_EVERY = 10          # seconds between sparkline samples
SPARK_POINTS = 360        # one hour
UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "pb": 1e15,
         "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4, "pib": 1024 ** 5}
SIZE_RE = re.compile(r"([\d.]+)\s*([a-zA-Z]+)")
HEALTH_RE = re.compile(r"\((healthy|unhealthy|health: starting)\)", re.I)
UP_RE = re.compile(r"^Up\s+(.*?)(?:\s*\(.*\))?$", re.I)


def parse_size(s):
    m = SIZE_RE.fullmatch((s or "").strip())
    if not m:
        return None
    return float(m.group(1)) * UNITS.get(m.group(2).lower(), 1)


def parse_pair(s):
    parts = (s or "").split("/")
    if len(parts) != 2:
        return None, None
    return parse_size(parts[0]), parse_size(parts[1])


def health_of(status):
    m = HEALTH_RE.search(status or "")
    if not m:
        return None
    h = m.group(1).lower()
    return "starting" if h == "health: starting" else h


def parse_up(status):
    """Approximate seconds since start from docker's 'Up 4 days' text (lower bound)."""
    m = UP_RE.match((status or "").strip())
    if not m:
        return None
    t = m.group(1).lower().strip()
    if t.startswith("less than a second"):
        return 0
    if t.startswith("about a minute"):
        return 60
    if t.startswith("about an hour"):
        return 3600
    m = re.match(r"(\d+)\s+(second|minute|hour|day|week|month|year)", t)
    if not m:
        return None
    mult = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800,
            "month": 2592000, "year": 31536000}[m.group(2)]
    return int(m.group(1)) * mult


def short_id(cid):
    """Unraid ids look like '<server>:<container>'; the stream sometimes adds escape junk."""
    cid = (cid or "").split(":")[-1]
    return re.sub(r"[^0-9a-f]", "", cid)


# --------------------------------------------------------------------------- stats stream

class StatsStream:
    def __init__(self, url, key_getter):
        u = urllib.parse.urlparse(url)
        self.host, self.port, self.path = u.hostname, u.port or 80, u.path or "/graphql"
        self.key_getter = key_getter
        self.lock = threading.Lock()
        self.latest = {}                                   # short id -> stats dict
        self.spark = collections.defaultdict(lambda: collections.deque(maxlen=SPARK_POINTS))
        self.connected = False
        self.error = None
        self.last_msg = 0

    # -- tiny websocket client
    def _send(self, s, obj, opcode=1):
        p = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(p)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(p)))

    def _run_once(self):
        key = self.key_getter()
        s = socket.create_connection((self.host, self.port), timeout=30)
        try:
            wskey = base64.b64encode(os.urandom(16)).decode()
            s.sendall((f"GET {self.path} HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\n"
                       f"Connection: Upgrade\r\nSec-WebSocket-Key: {wskey}\r\nSec-WebSocket-Version: 13\r\n"
                       f"Sec-WebSocket-Protocol: graphql-transport-ws\r\nx-api-key: {key}\r\n\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    raise ConnectionError("closed during handshake")
                buf += chunk
            head, buf = buf.split(b"\r\n\r\n", 1)
            if b" 101 " not in head.split(b"\r\n")[0]:
                raise ConnectionError(head.split(b"\r\n")[0].decode(errors="replace"))
            reader = {"buf": buf}

            def need(n):
                while len(reader["buf"]) < n:
                    chunk = s.recv(65536)
                    if not chunk:
                        raise ConnectionError("stream closed")
                    reader["buf"] += chunk

            def frame():
                need(2)
                b1, b2 = reader["buf"][0], reader["buf"][1]
                n, off = b2 & 127, 2
                if n == 126:
                    need(4)
                    n, off = struct.unpack("!H", reader["buf"][2:4])[0], 4
                elif n == 127:
                    need(10)
                    n, off = struct.unpack("!Q", reader["buf"][2:10])[0], 10
                need(off + n)
                p = reader["buf"][off:off + n]
                reader["buf"] = reader["buf"][off + n:]
                return bool(b1 & 0x80), b1 & 15, p

            self._send(s, {"type": "connection_init", "payload": {"x-api-key": key}})
            self._send(s, {"id": "1", "type": "subscribe", "payload": {"query":
                "subscription { dockerContainerStats { id cpuPercent memUsage memPercent netIO blockIO } }"}})
            s.settimeout(90)
            partial = b""
            while True:
                fin, op, p = frame()
                if op == 9:
                    self._send(s, p, opcode=10)
                    continue
                if op == 8:
                    raise ConnectionError("server closed the stream")
                if op in (0, 1):
                    partial += p
                    if not fin:
                        continue
                    msg, partial = json.loads(partial), b""
                    t = msg.get("type")
                    if t == "ping":
                        self._send(s, {"type": "pong"})
                    elif t == "next":
                        self._on_stats(((msg.get("payload") or {}).get("data") or {}).get("dockerContainerStats"))
                    elif t == "error":
                        raise RuntimeError("; ".join(e.get("message", "?") for e in msg.get("payload") or []))
                    elif t == "complete":
                        raise ConnectionError("subscription completed")
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _on_stats(self, d):
        if not d:
            return
        cid = short_id(d.get("id"))
        if not cid:
            return
        mem, mem_limit = parse_pair(d.get("memUsage"))
        rx, tx = parse_pair(d.get("netIO"))
        br, bw = parse_pair(d.get("blockIO"))
        now = time.time()
        with self.lock:
            self.latest[cid] = {"cpu": d.get("cpuPercent"), "mem": mem, "mem_limit": mem_limit,
                                "mem_pct": d.get("memPercent"), "net_rx": rx, "net_tx": tx,
                                "blk_r": br, "blk_w": bw, "ts": now}
            self.connected, self.error, self.last_msg = True, None, now

    def run(self):
        backoff = 2
        while True:
            try:
                self._run_once()
            except Exception as e:
                with self.lock:
                    self.connected = False
                    self.error = str(e) or e.__class__.__name__
                log.warning("docker stats stream: %s (reconnecting in %ss)", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            else:
                backoff = 2

    def sampler(self):
        while True:
            time.sleep(SPARK_EVERY)
            now = time.time()
            with self.lock:
                for cid, st in self.latest.items():
                    if now - st["ts"] < 3 * SPARK_EVERY:
                        self.spark[cid].append((round(now), st["cpu"], st["mem"]))
                for cid in [c for c, st in self.latest.items() if now - st["ts"] > 600]:
                    self.latest.pop(cid, None)
                    self.spark.pop(cid, None)

    def get(self, cid):
        with self.lock:
            st = self.latest.get(cid)
            sp = list(self.spark.get(cid) or [])
        if st and time.time() - st["ts"] > 60:
            st = None
        return st, sp

    def status(self):
        with self.lock:
            return {"connected": self.connected and time.time() - self.last_msg < 60,
                    "error": self.error, "last": self.last_msg or None}


# --------------------------------------------------------------------------- state history

class Tracker:
    """Records container state / health changes and restarts; computes uptime from them."""

    def __init__(self, db_query, db_write):
        self.q, self.w = db_query, db_write
        self.prev = {}            # name -> {state, health, up, t}
        self.manual = {}          # name -> ts of a restart we triggered (suppress double logging)
        self.lock = threading.Lock()
        for name, state, health in self.q(
                "SELECT e.name, e.state, e.health FROM cevents e JOIN "
                "(SELECT name, MAX(ts) AS ts FROM cevents GROUP BY name) l ON e.name = l.name AND e.ts = l.ts"):
            self.prev[name] = {"state": state, "health": health, "up": None, "t": None}

    def log(self, ts, name, kind, state, health, detail=""):
        self.w("INSERT INTO cevents (ts, name, kind, state, health, detail) VALUES (?, ?, ?, ?, ?, ?)",
               (ts, name, kind, state, health, detail))

    def note_manual_restart(self, name, who):
        now = time.time()
        with self.lock:
            self.manual[name] = now
            p = self.prev.get(name, {})
            self.log(now, name, "restart", "RUNNING", p.get("health"), f"from the dashboard by {who}")

    def observe(self, containers, now):
        with self.lock:
            seen = set()
            for c in containers:
                name, st, status = c["name"], (c.get("state") or "").upper(), c.get("status") or ""
                health, up = health_of(status), parse_up(status)
                seen.add(name)
                p = self.prev.get(name)
                if p is None:
                    self.log(now, name, "seen", st, health, f"{status}")
                elif p["state"] == "GONE":
                    self.log(now, name, "state", st, health, "container is back")
                elif p["state"] != st:
                    self.log(now, name, "state", st, health, status)
                else:
                    recent_manual = now - self.manual.get(name, 0) < 120
                    if (st == "RUNNING" and up is not None and p.get("up") is not None and p.get("t")
                            and up < 120 and p["up"] + (now - p["t"]) > 300 and not recent_manual):
                        self.log(now - up, name, "restart", st, health, "restarted (not from the dashboard)")
                    elif p.get("health") != health and health is not None and not (recent_manual and health == "starting"):
                        self.log(now, name, "health", st, health, "")
                self.prev[name] = {"state": st, "health": health, "up": up, "t": now}
            for name, p in list(self.prev.items()):
                if name not in seen and p["state"] != "GONE":
                    self.log(now, name, "state", "GONE", None, "container removed")
                    self.prev[name] = {"state": "GONE", "health": None, "up": None, "t": now}

    def _intervals(self, name, start, now):
        """[(t0, t1, running)] from the first tracked moment (or `start`) to now."""
        rows = self.q("SELECT ts, state FROM cevents WHERE name = ? AND ts >= ? AND kind != 'restart' ORDER BY ts",
                      (name, start))
        before = self.q("SELECT state FROM cevents WHERE name = ? AND ts < ? AND kind != 'restart' "
                        "ORDER BY ts DESC LIMIT 1", (name, start))
        out = []
        cur = (start, before[0][0]) if before else None
        for ts, st in rows:
            if cur is not None and ts > cur[0]:
                out.append((cur[0], ts, cur[1] == "RUNNING"))
            cur = (max(ts, cur[0]) if cur else ts, st)
        if cur is not None and now > cur[0]:
            out.append((cur[0], now, cur[1] == "RUNNING"))
        return out

    def uptime(self, name, now, status):
        res = {}
        iv30 = self._intervals(name, now - 2592000, now)
        for label, secs in (("24h", 86400), ("7d", 604800), ("30d", 2592000)):
            lo = now - secs
            tracked = up = 0
            for a, b, r in iv30:
                a = max(a, lo)
                if b <= a:
                    continue
                tracked += b - a
                if r:
                    up += b - a
            res[f"pct_{label}"] = up / tracked * 100 if tracked else None
            res[f"coverage_{label}"] = min(1.0, tracked / secs)
        # 24h strip, 48 half-hour cells
        width, lo = 1800, now - 86400
        cells = []
        for i in range(48):
            a0, b0 = lo + i * width, lo + (i + 1) * width
            tracked = up = 0
            for a, b, r in iv30:
                o = min(b, b0) - max(a, a0)
                if o > 0:
                    tracked += o
                    if r:
                        up += o
            cells.append({"start": a0, "end": b0, "tracked": round(tracked), "up": round(up)})
        res["strip"] = cells
        res["restarts_7d"] = self.q("SELECT COUNT(*) FROM cevents WHERE name = ? AND kind = 'restart' AND ts >= ?",
                                    (name, now - 604800))[0][0]
        # since: exact if we saw it start, else docker's rounded 'Up N days'
        last = self.q("SELECT ts FROM cevents WHERE name = ? AND (kind = 'restart' OR (kind = 'state' AND state = 'RUNNING')) "
                      "ORDER BY ts DESC LIMIT 1", (name,))
        up = parse_up(status)
        age = now - last[0][0] if last else None
        if last and (up is None or up - 120 <= age <= up * 1.5 + 3600):
            res["since"], res["since_exact"] = last[0][0], True
        elif up is not None:
            res["since"], res["since_exact"] = now - up, False
        else:
            down = self.q("SELECT ts FROM cevents WHERE name = ? AND kind = 'state' AND state != 'RUNNING' "
                          "ORDER BY ts DESC LIMIT 1", (name,))
            res["since"], res["since_exact"] = (down[0][0], True) if down else (None, False)
        return res

    def events(self, now, limit=60, name=None):
        if name:
            rows = self.q("SELECT ts, name, kind, state, health, detail FROM cevents WHERE name = ? "
                          "ORDER BY ts DESC LIMIT ?", (name, limit))
        else:
            rows = self.q("SELECT ts, name, kind, state, health, detail FROM cevents WHERE kind != 'seen' "
                          "ORDER BY ts DESC LIMIT ?", (limit,))
        return [{"ts": ts, "name": n, "kind": k, "state": s, "health": h, "detail": d} for ts, n, k, s, h, d in rows]
