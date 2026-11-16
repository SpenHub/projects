"""Radarr / Sonarr integration: library sizes and deleting releases through the *arr,
so its database stays in sync instead of pointing at files that vanished."""

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("girthy-dash.arr")

SEASON_RE = re.compile(r"^(?:season|series|staffel|saison|temporada)[ ._-]*0*(\d+)$", re.I)


class ArrError(Exception):
    pass


class Arr:
    def __init__(self, config_file):
        self.services = {}
        self.path_map = []
        self.config_error = None
        try:
            with open(config_file) as f:
                cfg = json.load(f)
            for name in ("radarr", "sonarr"):
                svc = cfg.get(name) or {}
                if svc.get("url") and svc.get("key"):
                    self.services[name] = {"url": svc["url"].rstrip("/"), "key": svc["key"]}
            self.path_map = [(a.rstrip("/"), b.rstrip("/")) for a, b in cfg.get("path_map", [])]
        except FileNotFoundError:
            self.config_error = f"no *arr config at {config_file}"
        except Exception as e:
            self.config_error = f"bad *arr config: {e}"
        self.lock = threading.Lock()
        self.cache = None
        self.cache_ts = 0

    def enabled(self, name):
        return name in self.services

    # -- http
    def req(self, svc, method, path, params=None, body=None, timeout=30):
        if svc not in self.services:
            raise ArrError(f"{svc} is not configured")
        cfg = self.services[svc]
        url = f"{cfg['url']}/api/v3{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method, headers={
            "X-Api-Key": cfg["key"], "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise ArrError(f"{svc.capitalize()} {method} {path} failed: HTTP {e.code} {detail}") from None
        except urllib.error.URLError as e:
            raise ArrError(f"can't reach {svc.capitalize()}: {e.reason}") from None
        return json.loads(raw) if raw.strip() else None

    # -- paths
    def to_host(self, p):
        p = (p or "").rstrip("/")
        for a, b in self.path_map:
            if p == a or p.startswith(a + "/"):
                return b + p[len(a):]
        return p

    # -- library
    def invalidate(self):
        with self.lock:
            self.cache = None

    def library(self, force=False):
        with self.lock:
            if not force and self.cache and time.time() - self.cache_ts < 60:
                return self.cache
        out = {"movies": [], "series": [], "recycle": {}, "errors": {},
               "enabled": {k: self.enabled(k) for k in ("radarr", "sonarr")}, "config_error": self.config_error}
        if self.enabled("radarr"):
            try:
                for m in self.req("radarr", "GET", "/movie"):
                    mf = m.get("movieFile") or {}
                    out["movies"].append({
                        "id": m["id"], "title": m.get("title"), "year": m.get("year"),
                        "path": self.to_host(m.get("path")), "size": m.get("sizeOnDisk") or mf.get("size") or 0,
                        "hasFile": bool(m.get("hasFile")), "monitored": bool(m.get("monitored")),
                        "quality": ((mf.get("quality") or {}).get("quality") or {}).get("name"),
                        "file": mf.get("relativePath"), "added": mf.get("dateAdded"),
                    })
                out["recycle"]["radarr"] = (self.req("radarr", "GET", "/config/mediamanagement") or {}).get("recycleBin") or ""
            except ArrError as e:
                out["errors"]["radarr"] = str(e)
        if self.enabled("sonarr"):
            try:
                for s in self.req("sonarr", "GET", "/series"):
                    st = s.get("statistics") or {}
                    out["series"].append({
                        "id": s["id"], "title": s.get("title"), "year": s.get("year"),
                        "path": self.to_host(s.get("path")), "size": st.get("sizeOnDisk") or 0,
                        "files": st.get("episodeFileCount") or 0, "monitored": bool(s.get("monitored")),
                        "status": s.get("status"),
                        "seasons": [{"n": x["seasonNumber"], "monitored": bool(x.get("monitored")),
                                     "size": (x.get("statistics") or {}).get("sizeOnDisk") or 0,
                                     "files": (x.get("statistics") or {}).get("episodeFileCount") or 0}
                                    for x in s.get("seasons") or []],
                    })
                out["recycle"]["sonarr"] = (self.req("sonarr", "GET", "/config/mediamanagement") or {}).get("recycleBin") or ""
            except ArrError as e:
                out["errors"]["sonarr"] = str(e)
        with self.lock:
            self.cache, self.cache_ts = out, time.time()
        return out

    def match(self, display_path, children):
        """Tag folders that belong to a Radarr movie or a Sonarr series / season."""
        lib = self.library()
        by_path = {}
        for m in lib["movies"]:
            by_path[m["path"]] = {"service": "radarr", "kind": "movie", "id": m["id"], "title": m["title"],
                                  "year": m["year"], "size": m["size"], "monitored": m["monitored"]}
        for s in lib["series"]:
            by_path[s["path"]] = {"service": "sonarr", "kind": "series", "id": s["id"], "title": s["title"],
                                  "year": s["year"], "size": s["size"], "monitored": s["monitored"]}
        own = by_path.get(display_path)
        parent_series = None
        for s in lib["series"]:
            if s["path"] == display_path:
                parent_series = s
        tags = {}
        for c in children:
            if c["type"] != "dir":
                continue
            child_path = display_path.rstrip("/") + "/" + c["name"]
            if child_path in by_path:
                tags[c["name"]] = by_path[child_path]
            elif parent_series is not None:
                m = SEASON_RE.match(c["name"])
                n = int(m.group(1)) if m else (0 if c["name"].lower() == "specials" else None)
                if n is not None:
                    season = next((x for x in parent_series["seasons"] if x["n"] == n), None)
                    tags[c["name"]] = {"service": "sonarr", "kind": "season", "id": parent_series["id"],
                                       "title": parent_series["title"], "season": n,
                                       "size": season["size"] if season else None,
                                       "monitored": season["monitored"] if season else None}
        return own, tags

    # -- actions
    def delete_movie(self, movie_id, mode, exclusion=False):
        m = self.req("radarr", "GET", f"/movie/{int(movie_id)}")
        title = f"{m.get('title')} ({m.get('year')})"
        if mode == "files":
            mf = m.get("movieFile")
            if mf:
                self.req("radarr", "DELETE", f"/moviefile/{mf['id']}")
            self.req("radarr", "PUT", "/movie/editor", body={"movieIds": [m["id"]], "monitored": False})
            msg = f"Deleted the file for {title}; Radarr keeps the movie, unmonitored."
        elif mode == "remove":
            self.req("radarr", "DELETE", f"/movie/{m['id']}",
                     params={"deleteFiles": "true", "addImportExclusion": "true" if exclusion else "false"})
            msg = f"Removed {title} from Radarr and deleted its files" + (" (added to exclusions)." if exclusion else ".")
        else:
            raise ArrError("unknown mode")
        self.invalidate()
        log.info("radarr: %s", msg)
        return self.to_host(m.get("path")), msg

    def delete_series(self, series_id, mode, exclusion=False, season=None):
        sid = int(series_id)
        s = self.req("sonarr", "GET", f"/series/{sid}")
        title = s.get("title")
        if mode == "remove":
            if season is not None:
                raise ArrError("a single season can't be removed from Sonarr; delete its files instead")
            self.req("sonarr", "DELETE", f"/series/{sid}",
                     params={"deleteFiles": "true", "addImportListExclusion": "true" if exclusion else "false"})
            msg = f"Removed {title} from Sonarr and deleted its files" + (" (added to exclusions)." if exclusion else ".")
        elif mode == "files":
            season = None if season is None else int(season)
            files = self.req("sonarr", "GET", "/episodefile", params={"seriesId": sid}) or []
            ids = [f["id"] for f in files if season is None or f.get("seasonNumber") == season]
            if ids:
                self.req("sonarr", "DELETE", "/episodefile/bulk", body={"episodeFileIds": ids}, timeout=120)
            eps = self.req("sonarr", "GET", "/episode", params={"seriesId": sid}) or []
            ep_ids = [e["id"] for e in eps if season is None or e.get("seasonNumber") == season]
            if ep_ids:
                self.req("sonarr", "PUT", "/episode/monitor", body={"episodeIds": ep_ids, "monitored": False})
            s = self.req("sonarr", "GET", f"/series/{sid}")
            for x in s.get("seasons") or []:
                if season is None or x.get("seasonNumber") == season:
                    x["monitored"] = False
            if season is None:
                s["monitored"] = False
            self.req("sonarr", "PUT", f"/series/{sid}", body=s)
            what = f"season {season} of {title}" if season is not None else title
            msg = f"Deleted {len(ids)} episode file{'s' if len(ids) != 1 else ''} from {what}; Sonarr keeps it, unmonitored."
        else:
            raise ArrError("unknown mode")
        self.invalidate()
        log.info("sonarr: %s", msg)
        return self.to_host(s.get("path")), msg
