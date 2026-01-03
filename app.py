# app.py — Metroboard backend (Rail + Metro) - FIXED LINE FILTERING
# - Uses static GTFS (stops.txt, routes.txt, trips.txt, stop_times.txt, agency.txt)
# - Filters to route_type in (1=subway/metro, 2=rail); optional agency allow-list
# - Builds station×route keys like "Central1" (T1) and "CentralM" (M1)
# - GTFS-RT from TfNSW: Sydney Trains + Sydney Metro vehicle positions
# - States logic: prefer VP.stop_id -> parent match; fallback to distance thresholds
# - **FIX**: Only match trains to stations with the EXACT same route_short
# - Endpoints: /, /healthz, /debug, /stations, /states, /trains

import os
import time
import csv
import json
import re
import random
import typing as T

import requests
from fastapi import FastAPI, Query, Request, Form
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from google.transit import gtfs_realtime_pb2 as rt
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- Config ----------------
API_TOKEN = os.getenv("TFNSW_API_TOKEN")

# Allow override via FEEDS env (comma-separated); default to both feeds
_FEEDS_ENV = os.getenv("FEEDS", "").strip()
if _FEEDS_ENV:
    URLS_VP = [u.strip() for u in _FEEDS_ENV.split(",") if u.strip()]
else:
    URLS_VP = [
        "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
        "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
    ]

# Distance thresholds (meters) for fallback
D_SOLID = int(os.getenv("MB_D_SOLID", "200"))
D_NEAR  = int(os.getenv("MB_D_NEAR",  "800"))
D_FAR   = int(os.getenv("MB_D_FAR",   "1500"))

# Allowlists (empty=set() means all)
AGENCY_NAME_ALLOW: set[str] = {a.strip() for a in os.getenv("MB_AGENCIES", "").split(",") if a.strip()}
ROUTE_SHORT_ALLOW: set[str] = {s.strip().upper() for s in os.getenv("MB_ROUTE_SHORT_ALLOW", "").split(",") if s.strip()}

# Route short-name overrides
ROUTE_SHORT_OVERRIDES_PATH = os.getenv("ROUTE_SHORT_OVERRIDES_PATH", "route_overrides.csv")
ROUTE_SHORT_OVERRIDES_JSON = os.getenv("ROUTE_SHORT_OVERRIDES_JSON", "")
BOARD_SETTINGS_PATH = os.getenv("BOARD_SETTINGS_PATH", "boards.json")

# board_id -> settings dict
BOARD_SETTINGS: dict[str, dict] = {}

# Caching/CORS/logging
FEED_CACHE_SEC = int(os.getenv("FEED_CACHE_SEC", "3"))
CORS_ALLOW_ORIGIN = os.getenv("CORS_ALLOW_ORIGIN")
MB_VERBOSE = os.getenv("MB_VERBOSE", "1") == "1"
MB_FORCE_DISTANCE = os.getenv("MB_FORCE_DISTANCE", "0") == "1"

# BBox guardrail (None disables)
DEFAULT_SYD_BBOX = (-34.2, 150.5, -33.4, 151.4)
def _bbox_env() -> T.Optional[tuple[float, float, float, float]]:
    if os.getenv("SYD_BBOX_DISABLE") == "1":
        return None
    s = os.getenv("SYD_BBOX")
    if not s:
        return DEFAULT_SYD_BBOX
    try:
        a, b, c, d = [float(x) for x in s.split(",")]
        return (a, b, c, d)
    except Exception:
        return DEFAULT_SYD_BBOX

SYD_BBOX = _bbox_env()

def vlog(*a):
    if MB_VERBOSE:
        print("[metroboard]", *a)

def _load_board_settings() -> dict[str, dict]:
    if not os.path.exists(BOARD_SETTINGS_PATH):
        return {}
    try:
        with open(BOARD_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # Backward-compat: older boards.json stored brightness as a dict.
            for bid, cfg in data.items():
                if not isinstance(cfg, dict):
                    continue
                b = cfg.get("brightness")
                if isinstance(b, dict):
                    # Prefer live brightness, fall back to animation.
                    live = b.get("live")
                    anim = b.get("animation")
                    if isinstance(live, (int, float)):
                        cfg["brightness"] = int(live)
                    elif isinstance(anim, (int, float)):
                        cfg["brightness"] = int(anim)
                elif isinstance(b, (int, float)):
                    cfg["brightness"] = int(b)
            return data
    except Exception as e:
        vlog("WARN: failed to load boards.json:", e)
    return {}

def _save_board_settings() -> None:
    try:
        with open(BOARD_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(BOARD_SETTINGS, f, indent=2, sort_keys=True)
    except Exception as e:
        vlog("WARN: failed to save boards.json:", e)

def _default_board_settings(board_id: str) -> dict:
    return {
        "board_id": board_id,
        "brightness": 160,
        "mode": "live",  # "live" or "animation"
        "night_mode": {
            "enabled": False,
            "start": "23:00",
            "end": "06:00",
            "action": "off",  # "off" or "dim"
            "dim_level": 20
        },
        "animation": {
            "name": "pulse",
            "speed": 120,
            "color": [255, 80, 0]
        },
    }

def _get_or_create_board(board_id: str) -> dict:
    board_id = (board_id or "").strip()
    if not board_id:
        raise ValueError("empty board_id")
    if board_id not in BOARD_SETTINGS:
        BOARD_SETTINGS[board_id] = _default_board_settings(board_id)
        _save_board_settings()
    return BOARD_SETTINGS[board_id]

EXCLUDE_SUB = ("wharf", "light rail", "b-line", "interchange", "motorway", " m2")
SHORT_RE = re.compile(r"\b([TM]\d{1,2})\b", re.I)

# ---------------- Small helpers ----------------
def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=0.6,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

def _valid_ll(x: float, y: float) -> bool:
    try:
        x = float(x); y = float(y)
    except Exception:
        return False
    return -90 <= x <= 90 and -180 <= y <= 180

def hav_m(a: float, b: float, c: float, d: float) -> float:
    try:
        a = float(a); b = float(b); c = float(c); d = float(d)
    except Exception:
        return float("inf")
    if not (_valid_ll(a, b) and _valid_ll(c, d)):
        return float("inf")
    from math import radians, sin, cos, asin, sqrt
    R = 6371000.0
    da = radians(c - a)
    db = radians(d - b)
    t = sin(da / 2) ** 2 + cos(radians(a)) * cos(radians(c)) * sin(db / 2) ** 2
    return 2 * R * asin(sqrt(max(0.0, min(1.0, t))))

def within_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    S, W, N, E = bbox
    return (S <= lat <= N) and (W <= lon <= E)

def is_num_parent(s: str) -> bool:
    return s.isdigit()

def status_from_dist(m: T.Optional[float]) -> int:
    if m is None:
        return 0
    if m <= D_SOLID:
        return 3  # solid/stopped
    if m <= D_NEAR:
        return 2  # approaching/medium
    if m <= D_FAR:
        return 1  # in area/dim
    return 0

def clean_name(n: str) -> str:
    n = (n or "").strip()
    for suf in (" Station", " Railway Station"):
        if n.endswith(suf):
            n = n[: -len(suf)]
    return n

def _is_stationish(n: str) -> bool:
    n = (n or "").strip().lower()
    if any(s in n for s in EXCLUDE_SUB):
        return False
    return n.endswith(" station") or n.endswith(" railway station")

def load_whitelist(path: str) -> tuple[set[str], set[str]]:
    sid_w: set[str] = set()
    name_w: set[str] = set()
    if not path or not os.path.exists(path):
        return sid_w, name_w
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            token = line.split(",", 1)[0].strip()
            if token.isdigit():
                sid_w.add(token)
            else:
                name_w.add(clean_name(token).lower())
    return sid_w, name_w

# ---------------- Route short names (with overrides) ----------------
def derive_route_short(route_short_name: str | None, route_long_name: str | None) -> str:
    rs = (route_short_name or "").strip()
    if rs:
        return rs.upper()
    rl = (route_long_name or "").strip()
    if rl:
        m = SHORT_RE.search(rl)
        if m:
            return m.group(1).upper()
    return ""

def load_route_overrides(path: str, env_json: str) -> dict[str, str]:
    out: dict[str, str] = {}
    # CSV file
    try:
        if path and os.path.exists(path):
            with open(path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    rid = (row.get("route_id") or "").strip()
                    rs = (row.get("route_short_name") or "").strip().upper()
                    if rid and rs:
                        out[rid] = rs
            vlog(f"route_overrides: loaded {len(out)} from file")
    except Exception as e:
        vlog(f"WARN: override file: {e}")
    # Env JSON
    if env_json:
        try:
            obj = json.loads(env_json)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    k = (k or "").strip()
                    v = (v or "").strip().upper()
                    if k and v:
                        out[k] = v
            vlog("route_overrides: merged env JSON")
        except Exception as e:
            vlog(f"WARN: override env: {e}")
    return out

def load_route_shortnames(routes_path: str = "routes.txt") -> dict[str, str]:
    ov = load_route_overrides(ROUTE_SHORT_OVERRIDES_PATH, ROUTE_SHORT_OVERRIDES_JSON)
    out: dict[str, str] = {}
    with open(routes_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid = (r.get("route_id") or "").strip()
            if not rid:
                continue
            rs = ov.get(rid) or derive_route_short(r.get("route_short_name"), r.get("route_long_name"))
            if rs:
                out[rid] = rs
    vlog(f"route_short: {len(out)} (ov={len(ov)})")
    return out

# ---------------- Static GTFS filters: allowed parents & pairs ----------------
def require_allowed_parents(
    stops="stops.txt",
    routes="routes.txt",
    trips="trips.txt",
    stop_times="stop_times.txt",
    agency="agency.txt",
    types=(1, 2),
    agencies_allowed: set[str] = AGENCY_NAME_ALLOW,
    route_short_allow: set[str] = ROUTE_SHORT_ALLOW,
) -> set[str]:
    missing = [p for p in (stops, routes, trips, stop_times, agency) if not os.path.exists(p)]
    if missing:
        raise RuntimeError("Missing GTFS: " + ", ".join(missing))

    ag_name: dict[str, str] = {}
    with open(agency, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ag_name[(r.get("agency_id") or "").strip()] = (r.get("agency_name") or "").strip()

    route_meta: dict[str, tuple[int, str, str]] = {}
    with open(routes, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid = (r.get("route_id") or "").strip()
            try:
                rtype = int((r.get("route_type") or "3").strip() or "3")
            except Exception:
                rtype = 3
            rshort = (r.get("route_short_name") or "").strip()
            agid = (r.get("agency_id") or "").strip()
            route_meta[rid] = (rtype, rshort, ag_name.get(agid, ""))

    trip_route: dict[str, str] = {}
    with open(trips, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            trip_route[(r.get("trip_id") or "").strip()] = (r.get("route_id") or "").strip()

    stops_map: dict[str, dict] = {}
    parents: set[str] = set()
    with open(stops, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("stop_id") or "").strip()
            stops_map[sid] = r
            if (r.get("location_type") or "0").strip() == "1":
                parents.add(sid)

    def parent_of(sid: str) -> T.Optional[str]:
        row = stops_map.get(sid)
        if not row:
            return None
        p = (row.get("parent_station") or "").strip()
        if p:
            return p
        return sid if sid in parents else None

    allowed: set[str] = set()
    seen_types, seen_ag = set(), set()
    with open(stop_times, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid = trip_route.get((r.get("trip_id") or "").strip(), "")
            rtype, rshort, ag = route_meta.get(rid, (None, "", ""))
            if rtype is None:
                continue
            seen_types.add(rtype); seen_ag.add(ag)
            if types and rtype not in types:
                continue
            if agencies_allowed and ag not in agencies_allowed:
                continue
            if route_short_allow and rshort and rshort.upper() not in route_short_allow:
                continue
            p = parent_of((r.get("stop_id") or "").strip())
            if p and is_num_parent(p):
                allowed.add(p)

    vlog("routes types seen", sorted(seen_types), "agencies", sorted(seen_ag))
    if allowed:
        vlog(f"parents by trips: {len(allowed)}")
        return allowed

    # fallback: names
    fb: set[str] = set()
    with open(stops, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("location_type") or "0").strip() != "1":
                continue
            pid = (r.get("stop_id") or "").strip()
            if not is_num_parent(pid):
                continue
            if _is_stationish((r.get("stop_name") or "")):
                fb.add(pid)
    if not fb:
        raise RuntimeError("No parent stations via fallback.")
    vlog(f"parents by names: {len(fb)}")
    return fb

def _known_tm(route_id_to_short: dict[str, str]) -> set[str]:
    out: set[str] = set()
    for rs in route_id_to_short.values():
        u = (rs or "").upper().strip()
        if re.fullmatch(r"[TM]\d{1,2}", u):
            out.add(u)
    return out

def require_allowed_pairs(
    stops="stops.txt",
    routes="routes.txt",
    trips="trips.txt",
    stop_times="stop_times.txt",
    agency="agency.txt",
    types=(1, 2),
    agencies_allowed: set[str] = AGENCY_NAME_ALLOW,
    route_short_allow: set[str] = ROUTE_SHORT_ALLOW,
    allowed_parents_for_fallback: T.Optional[set[str]] = None,
) -> set[tuple[str, str]]:
    ag_name: dict[str, str] = {}
    with open(agency, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ag_name[(r.get("agency_id") or "").strip()] = (r.get("agency_name") or "").strip()

    rt_meta: dict[str, tuple[int, str, str]] = {}
    with open(routes, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid = (r.get("route_id") or "").strip()
            try:
                rtype = int((r.get("route_type") or "3").strip() or "3")
            except Exception:
                rtype = 3
            agid = (r.get("agency_id") or "").strip()
            ag = ag_name.get(agid, "")
            rlong = (r.get("route_long_name") or "").strip()
            rt_meta[rid] = (rtype, ag, rlong)

    trip_route: dict[str, str] = {}
    with open(trips, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            trip_route[(r.get("trip_id") or "").strip()] = (r.get("route_id") or "").strip()

    stops_map: dict[str, dict] = {}
    parents: set[str] = set()
    with open(stops, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("stop_id") or "").strip()
            stops_map[sid] = r
            if (r.get("location_type") or "0").strip() == "1":
                parents.add(sid)

    def parent_of(sid: str) -> T.Optional[str]:
        row = stops_map.get(sid)
        if not row:
            return None
        p = (row.get("parent_station") or "").strip()
        if p:
            return p
        return sid if sid in parents else None

    route_id_to_short = load_route_shortnames(routes)

    pairs: set[tuple[str, str]] = set()
    missing_short_seen: dict[str, int] = {}

    with open(stop_times, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid = trip_route.get((r.get("trip_id") or "").strip(), "")
            rtype, ag, _ = rt_meta.get(rid, (None, "", ""))
            if rtype is None:
                continue
            if types and rtype not in types:
                continue
            if agencies_allowed and ag not in agencies_allowed:
                continue
            rshort = (route_id_to_short.get(rid, "") or "").upper()
            if not rshort:
                if len(missing_short_seen) < 20:
                    missing_short_seen[rid] = missing_short_seen.get(rid, 0) + 1
                continue
            if route_short_allow and rshort not in route_short_allow:
                continue
            p = parent_of((r.get("stop_id") or "").strip())
            if p and is_num_parent(p):
                pairs.add((p, rshort))

    if pairs:
        vlog(f"pairs strict: {len(pairs)}")
        return pairs

    # permissive fallback
    if os.getenv("MB_PERMISSIVE_PAIRS", "1") != "0":
        known = _known_tm(route_id_to_short)
        if route_short_allow:
            known &= {x.upper() for x in route_short_allow}
        if not known:
            vlog("WARN: no T*/M* for fallback")
            return set()
        if not allowed_parents_for_fallback:
            allowed_parents_for_fallback = {
                sid for sid, row in stops_map.items()
                if (row.get("location_type") or "0").strip() == "1" and is_num_parent(sid)
            }
        fan = {(pid, rs) for pid in allowed_parents_for_fallback for rs in known}
        vlog(f"pairs fallback: {len(fan)}")
        return fan

    if missing_short_seen:
        vlog("WARN: zero pairs; sample route_ids with no short code:")
        for rid in list(missing_short_seen.keys())[:10]:
            meta = rt_meta.get(rid, (None, "", ""))
            vlog(f"  - rid={rid}  agency={meta[1]!r}  rtype={meta[0]} (add override)")
    raise RuntimeError("No station×route pairs; supply overrides or enable fallback.")

# ---------------- Build station index (per station × route) ----------------
def route_suffix(rs: str) -> str:
    rs = (rs or "").upper()
    if rs.startswith("T") and len(rs) >= 2:
        return rs[1:]
    if rs.startswith("M"):
        return "M"
    return rs or "?"

def load_station_index(
    stops="stops.txt",
    bbox: T.Optional[tuple[float, float, float, float]] = SYD_BBOX,
    sid_whitelist: set[str] = set(),
    name_whitelist: set[str] = set(),
    allowed_parents: T.Optional[set[str]] = None,
    allowed_pairs: T.Optional[set[tuple[str, str]]] = None,
) -> dict[str, dict]:
    if allowed_parents is None or allowed_pairs is None:
        raise RuntimeError("allowed_parents and allowed_pairs are required")

    parents: dict[str, dict] = {}
    kids: dict[str, list[tuple[float, float]]] = {}

    with open(stops, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            lt = (r.get("location_type") or "0").strip()
            sid = (r.get("stop_id") or "").strip()
            parent = (r.get("parent_station") or "").strip()
            name_raw = (r.get("stop_name") or "").strip()
            lat_s, lon_s = r.get("stop_lat"), r.get("stop_lon")
            lat = float(lat_s) if lat_s else None
            lon = float(lon_s) if lon_s else None

            if lt == "1":
                if is_num_parent(sid) and sid in allowed_parents and not any(x in name_raw.lower() for x in EXCLUDE_SUB):
                    parents[sid] = {"name": name_raw, "lat": lat, "lon": lon}
            else:
                if parent and lat is not None and lon is not None:
                    if (bbox is None) or within_bbox(lat, lon, bbox):
                        kids.setdefault(parent, []).append((lat, lon))

    by_parent: dict[str, set[str]] = {}
    for pid, rs in allowed_pairs:
        by_parent.setdefault(pid, set()).add(rs)

    idx: dict[str, dict] = {}
    for pid, meta in parents.items():
        base = clean_name(meta["name"])
        lat, lon = meta["lat"], meta["lon"]
        if (lat is None or lon is None) and pid in kids and kids[pid]:
            lat = sum(t[0] for t in kids[pid]) / len(kids[pid])
            lon = sum(t[1] for t in kids[pid]) / len(kids[pid])
        if lat is None or lon is None:
            continue
        if (sid_whitelist or name_whitelist):
            if not (pid in sid_whitelist or base.lower() in name_whitelist):
                continue
        if SYD_BBOX is not None and not within_bbox(lat, lon, SYD_BBOX) and pid not in kids:
            continue

        for rs in sorted(by_parent.get(pid, set())):
            suf = route_suffix(rs)
            key = f"{base}{suf}"
            if key in idx:
                key = f"{base}{suf} ({pid})"
            idx[key] = {"stop_id": pid, "name": key, "lat": lat, "lon": lon, "route_short": rs, "base_name": base}

    return idx

# ---------------- Child→Parent map for VP.stop_id resolution ----------------
def build_child_to_parent(stops="stops.txt") -> dict[str, str]:
    out: dict[str, str] = {}
    with open(stops, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("stop_id") or "").strip()
            pid = (r.get("parent_station") or "").strip()
            lt = (r.get("location_type") or "0").strip()
            if sid and pid and is_num_parent(pid):
                out[sid] = pid
            if sid and lt == "1" and is_num_parent(sid):
                out.setdefault(sid, sid)
    vlog(f"child→parent: {len(out)}")
    return out

# ---------------- GTFS-RT: fetch & parse ----------------
_FEED_CACHE: T.Optional[rt.FeedMessage] = None
_FEED_TS = 0.0

def fetch_vp_cached() -> tuple[rt.FeedMessage, float]:
    global _FEED_CACHE, _FEED_TS
    now = time.time()
    if _FEED_CACHE is not None and (now - _FEED_TS) < FEED_CACHE_SEC:
        return _FEED_CACHE, (now - _FEED_TS)

    if not API_TOKEN:
        raise RuntimeError("TFNSW_API_TOKEN is not configured; live feeds unavailable")

    hdr = {
        "Authorization": f"apikey {API_TOKEN}",
        "Accept": "application/x-protobuf",
        "Cache-Control": "no-cache",
        "User-Agent": "metroboard/1.0",
    }
    s = _session()
    time.sleep(random.uniform(0, 0.25))  # stagger

    merged = rt.FeedMessage()
    merged.header.gtfs_realtime_version = "2.0"
    total = 0

    for url in URLS_VP:
        try:
            r = s.get(url, headers=hdr, timeout=20)
            r.raise_for_status()
            feed = rt.FeedMessage()
            feed.ParseFromString(r.content)
            veh = sum(1 for e in feed.entity if e.HasField("vehicle"))
            merged.entity.extend(feed.entity)
            total += veh
            vlog(f"fetched {url}: vehicles={veh}")
        except Exception as e:
            vlog("WARN fetch", url, e)

    _FEED_CACHE = merged
    _FEED_TS = now
    vlog("feed merged vehicles", total)
    return merged, 0.0

def load_station_route_pairs(path: str = "station_line_pairs.csv") -> dict[str, set[str]]:
    """Load parent_stop_id -> set of route_short from station_line_pairs.csv"""
    pairs: dict[str, set[str]] = {}
    if not os.path.exists(path):
        vlog(f"WARN: {path} not found, route inference will be limited")
        return pairs
    
    try:
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pid = (row.get("parent_stop_id") or "").strip()
                rs = (row.get("route_short") or "").strip().upper()
                if pid and rs:
                    pairs.setdefault(pid, set()).add(rs)
        vlog(f"Loaded {len(pairs)} station->route mappings from {path}")
    except Exception as e:
        vlog(f"WARN: Failed to load {path}: {e}")
    
    return pairs

# Call this at startup
STATION_ROUTE_PAIRS: dict[str, set[str]] = {}

def collect_trains(feed: rt.FeedMessage, route_id_to_short: dict[str, str]) -> tuple[
    list[tuple[T.Optional[float], T.Optional[float], T.Optional[str], str, int]],
    dict[str, int],
    dict[str, str],
]:
    """Return (trains, counts, route_debug)."""
    trains = []
    by_route = {}
    missing_routes = {}
    route_debug = {}

    # COMPREHENSIVE route prefix mapping - INCLUDING T8!
    ROUTE_PREFIX_MAP = {
        # Core Sydney Trains lines
        "BMT": ["T1"],           # Blue Mountains Line
        "NTH": ["T1", "T9"],     # North Shore Line
        "WST": ["T1"],           # Western Line  
        "APS": ["T8"],           # Airport & South Line - FIXED! Was wrongly T1/T9
        "CTY": ["T2"],           # Inner West & Leppington
        "T3": ["T3"],            # Bankstown Line
        "IWL": ["T4"],           # Illawarra Line (via Illawarra)
        "ESI": ["T4"],           # Eastern Suburbs & Illawarra
        "CMB": ["T5"],           # Cumberland Line
        "RTTA": ["T6"],          # Carlingford/Lidcombe
        "T6": ["T6"],            # Lidcombe & Bankstown (new)
        "OLY": ["T7"],           # Olympic Park Line
        "T7": ["T7"],            # Olympic Park
        "APT": ["T8"],           # Airport Line
        "SHL": ["T8"],           # South/Airport Line
        "AIRS": ["T8"],          # Airport & South
        "AIR": ["T8"],           # Airport
        "ESS": ["T8"],           # Eastern Suburbs South
        "T8": ["T8"],            # Direct T8
        "NSN": ["T9"],           # Northern Line
        
        # Regional lines
        "SCO": ["SCO"],          # South Coast Line
        "CCN": ["CCN"],          # Central Coast & Newcastle
        "SOUTHCOAST": ["SCO"],
        "CENTRALCOAST": ["CCN"],
        "WOLLONGONG": ["SCO"],
        
        # Metro
        "SMNW": ["M1"],          # Sydney Metro Northwest
        "M1": ["M1"],            # Metro M1
        "METRO": ["M1"],
        
        # Other patterns
        "MOUNTAINS": ["T1"],
        "CUMBERLAND": ["T5"],
        "LIVERPOOL": ["T3"],
        "SEFTON": ["T3", "T6"],
        "BLACKTOWN": ["T1"],
        "LIDCOMBE": ["T1", "T7"],
        "BANKSTOWN": ["T3"],
        "EASTERN": ["T4", "T8"],
        "ILLAWARRA": ["T4"],
        
        # Defensive catch
        "": ["T1"],
    }

    for e in feed.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        rid = v.trip.route_id if v.HasField("trip") else ""
        tripid = v.trip.trip_id if v.HasField("trip") else ""
        sid = getattr(v, "stop_id", "")

        # Resolve stop_id to parent station ID early for use in disambiguation
        parent = None
        if sid and STATION_ROUTE_PAIRS:
            parent = CHILD_TO_PARENT.get(sid) or (sid if is_num_parent(sid) else None)

        # METHOD 1: Use static GTFS route mapping (most reliable)
        rs = (route_id_to_short.get(rid, "") or "").upper()

        # METHOD 2: Extract T/M code from route_id or trip_id
        if not rs:
            for src in (rid, tripid):
                if not src:
                    continue
                m = re.search(r"\b([TM]\d{1,2})\b", src, re.I)
                if m:
                    rs = m.group(1).upper()
                    break

        # METHOD 3: Use prefix mapping with station-based disambiguation
        if not rs and rid:
            prefix = rid.split("_", 1)[0].split(".")[0].upper()
            candidates = []
            
            # Try exact match first
            if prefix in ROUTE_PREFIX_MAP:
                candidates = ROUTE_PREFIX_MAP[prefix]
            else:
                # Try substring match
                for key, opts in ROUTE_PREFIX_MAP.items():
                    if key and key in prefix:
                        candidates = opts
                        break
            
            # If we have candidates, use station info to pick the right one
            if candidates and len(candidates) > 1 and parent and parent in STATION_ROUTE_PAIRS:
                # Filter candidates to only those that serve this station
                possible = set(candidates) & STATION_ROUTE_PAIRS[parent]
                if len(possible) == 1:
                    rs = list(possible)[0]
                    if MB_VERBOSE:
                        vlog(f"Disambiguated {rid} at {parent}: {candidates} -> {rs}")
                elif possible:
                    # Multiple matches, use heuristics
                    if any(x in rid.upper() for x in ["AIR", "SHL", "APT", "ESS"]) and "T8" in possible:
                        rs = "T8"
                    else:
                        rs = sorted(possible)[0]
                else:
                    # None of the candidates serve this station - station data might be wrong
                    # Fall back to hash-based selection
                    rs = candidates[abs(hash(tripid or rid)) % len(candidates)]
            elif candidates:
                rs = candidates[0] if len(candidates) == 1 else candidates[abs(hash(tripid or rid)) % len(candidates)]

        # METHOD 4: Infer from stop_id using station_line_pairs.csv
        # Only used as absolute last resort
        if not rs and parent and parent in STATION_ROUTE_PAIRS:
            possible_routes = STATION_ROUTE_PAIRS[parent]
            if len(possible_routes) == 1:
                rs = list(possible_routes)[0]
                if MB_VERBOSE:
                    vlog(f"Inferred route {rs} from stop {parent} (only option)")
            elif len(possible_routes) > 1:
                # Prefer T8 if AIR/SHL keywords present
                if any(x in rid.upper() for x in ["AIR", "SHL", "APT", "ESS"]) and "T8" in possible_routes:
                    rs = "T8"
                else:
                    rs = sorted(possible_routes)[0]
                if MB_VERBOSE:
                    vlog(f"Inferred route {rs} from stop {parent} (chose from {possible_routes})")

        rs = rs or None
        route_debug[rid] = rs
        if not rs and rid:
            missing_routes[rid] = missing_routes.get(rid, 0) + 1

        if rs and ROUTE_SHORT_ALLOW and rs not in ROUTE_SHORT_ALLOW:
            continue

        status = int(getattr(v, "current_status", rt.VehiclePosition.IN_TRANSIT_TO))
        lat = getattr(v.position, "latitude", None) if v.HasField("position") else None
        lon = getattr(v.position, "longitude", None) if v.HasField("position") else None
        trains.append((lat, lon, rs, sid, status))
        if rs:
            by_route[rs] = by_route.get(rs, 0) + 1

    vlog(f"collect_trains kept={len(trains)}, routes={len(by_route)}, null_routes={len([t for t in trains if t[2] is None])}")
    if missing_routes:
        vlog(f"Missing route mappings (top 10): {dict(sorted(missing_routes.items(), key=lambda x: -x[1])[:10])}")
    
    return trains, by_route, route_debug


# ---------------- FastAPI app ----------------
app = FastAPI()

if CORS_ALLOW_ORIGIN:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in CORS_ALLOW_ORIGIN.split(",")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

ROUTE_ID_TO_SHORT: dict[str, str] = {}
STATION_INDEX: dict[str, dict] = {}
CHILD_TO_PARENT: dict[str, str] = {}
STATION_ROUTE_PAIRS: dict[str, set[str]] = {}

@app.on_event("startup")
def _startup():
    global ROUTE_ID_TO_SHORT, STATION_INDEX, CHILD_TO_PARENT, BOARD_SETTINGS, STATION_ROUTE_PAIRS

    BOARD_SETTINGS = _load_board_settings()

    ROUTE_ID_TO_SHORT = load_route_shortnames("routes.txt")

    allowed_parents = require_allowed_parents(
        stops="stops.txt",
        routes="routes.txt",
        trips="trips.txt",
        stop_times="stop_times.txt",
        agency="agency.txt",
        types=(1, 2),
        agencies_allowed=AGENCY_NAME_ALLOW,
        route_short_allow=ROUTE_SHORT_ALLOW,
    )

    allowed_pairs = require_allowed_pairs(
        stops="stops.txt",
        routes="routes.txt",
        trips="trips.txt",
        stop_times="stop_times.txt",
        agency="agency.txt",
        types=(1, 2),
        agencies_allowed=AGENCY_NAME_ALLOW,
        route_short_allow=ROUTE_SHORT_ALLOW,
        allowed_parents_for_fallback=allowed_parents,
    )

    wl_path = os.getenv("STATION_WHITELIST_PATH", "stations_whitelist.txt")
    sid_w, name_w = load_whitelist(wl_path)

    STATION_INDEX = load_station_index(
        "stops.txt",
        bbox=SYD_BBOX,
        sid_whitelist=sid_w,
        name_whitelist=name_w,
        allowed_parents=allowed_parents,
        allowed_pairs=allowed_pairs,
    )

    CHILD_TO_PARENT = build_child_to_parent("stops.txt")
    
    # Load station->route mappings for route inference
    STATION_ROUTE_PAIRS = load_station_route_pairs("station_line_pairs.csv")

    if not STATION_INDEX:
        raise RuntimeError("No stations after filtering")
    vlog(f"stations loaded: {len(STATION_INDEX)}")

@app.get("/")
def root():
    return {"ok": True, "endpoints": ["/states", "/stations", "/healthz", "/debug", "/trains"]}

@app.get("/healthz")
def healthz():
    age = None if _FEED_TS == 0 else (time.time() - _FEED_TS)
    return {"status": "ok", "stations": len(STATION_INDEX), "trains_last_fetch_s": age}

@app.get("/login", response_class=HTMLResponse)
def login_get():
    return """
    <html>
      <head><title>Metroboard Login</title></head>
      <body>
        <h2>Metroboard Login</h2>
        <form method="post" action="/login">
          <label>Board ID (5-char code):</label>
          <input type="text" name="board_id" minlength="5" maxlength="16" required />
          <button type="submit">Open</button>
        </form>
      </body>
    </html>
    """

@app.post("/login")
async def login_post(board_id: str = Form(...)):
    board_id = board_id.strip()
    if not board_id:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse(f"/board/{board_id}", status_code=303)


@app.get("/board/{board_id}", response_class=HTMLResponse)
def board_settings_page(board_id: str):
    try:
        cfg = _get_or_create_board(board_id)
    except ValueError:
        return HTMLResponse("Invalid board_id", status_code=400)

    b = cfg.get("brightness", 160)
    mode = cfg.get("mode", "live")
    nm = cfg.get("night_mode", {}) or {}
    an = cfg.get("animation", {}) or {}

    nm_enabled = "checked" if nm.get("enabled") else ""
    nm_start = nm.get("start", "23:00")
    nm_end = nm.get("end", "06:00")
    nm_action = nm.get("action", "off")
    nm_dim = nm.get("dim_level", 20)

    an_name = an.get("name", "pulse")
    an_speed = an.get("speed", 120)
    an_color = an.get("color", [255, 80, 0])
    an_color_hex = "#%02X%02X%02X" % (an_color[0], an_color[1], an_color[2])

    return f"""
    <html>
      <head>
        <title>Metroboard {board_id} Settings</title>
      </head>
      <body>
        <h2>Metroboard {board_id}</h2>
        <form method="post" action="/board/{board_id}">
          <h3>Display Mode</h3>
          <label><input type="radio" name="mode" value="live" {'checked' if mode=='live' else ''}> Live trains</label><br>
          <label><input type="radio" name="mode" value="animation" {'checked' if mode=='animation' else ''}> Animation</label>

          <h3>Brightness</h3>
          <input type="range" min="0" max="255" name="brightness" value="{b}" />

          <h3>Night Mode</h3>
          <label>
            <input type="checkbox" name="nm_enabled" {nm_enabled}> Enable night mode
          </label><br>
          <label>Start: <input type="time" name="nm_start" value="{nm_start}"></label><br>
          <label>End: <input type="time" name="nm_end" value="{nm_end}"></label><br>
          <label>Action:
            <select name="nm_action">
              <option value="off" {"selected" if nm_action=="off" else ""}>Turn off</option>
              <option value="dim" {"selected" if nm_action=="dim" else ""}>Dim</option>
            </select>
          </label><br>
          <label>Night brightness:
            <input type="number" name="nm_dim" min="0" max="255" value="{nm_dim}">
          </label>

          <h3>Animation</h3>
          <label>Name:
            <select name="an_name">
              <option value="pulse" {"selected" if an_name=="pulse" else ""}>Pulse</option>
              <option value="rainbow" {"selected" if an_name=="rainbow" else ""}>Rainbow</option>
            </select>
          </label><br>
          <label>Speed:
            <input type="number" name="an_speed" value="{an_speed}">
          </label><br>
          <label>Colour:
            <input type="color" name="an_color" value="{an_color_hex}">
          </label><br><br>

          <button type="submit">Save</button>
        </form>

        <p><a href="/login">Back</a></p>
      </body>
    </html>
    """


@app.post("/board/{board_id}")
async def board_settings_save(
    board_id: str,
    mode: str = Form("live"),
    brightness: int = Form(160),
    nm_enabled: T.Optional[str] = Form(None),
    nm_start: str = Form("23:00"),
    nm_end: str = Form("06:00"),
    nm_action: str = Form("off"),
    nm_dim: int = Form(20),
    an_name: str = Form("pulse"),
    an_speed: int = Form(120),
    an_color: str = Form("#FF5000"),
):
    cfg = _get_or_create_board(board_id)

    mode = mode if mode in ("live", "animation") else "live"
    brightness = max(0, min(255, brightness))
    nm_dim = max(0, min(255, nm_dim))

    def _parse_hex_color(s: str) -> list[int]:
        s = s.lstrip("#")
        if len(s) == 3: s = "".join(c*2 for c in s)
        if len(s) != 6: return [255, 80, 0]
        return [int(s[i:i+2], 16) for i in (0, 2, 4)]

    cfg["mode"] = mode
    cfg["brightness"] = brightness
    cfg["night_mode"] = {
        "enabled": bool(nm_enabled),
        "start": nm_start,
        "end": nm_end,
        "action": nm_action,
        "dim_level": nm_dim,
    }
    cfg["animation"] = {
        "name": an_name,
        "speed": an_speed,
        "color": _parse_hex_color(an_color),
    }

    BOARD_SETTINGS[board_id] = cfg
    _save_board_settings()

    return RedirectResponse(f"/board/{board_id}", status_code=303)

@app.get("/debug")
def debug():
    feed, _ = fetch_vp_cached()
    trains, by_route, route_debug = collect_trains(feed, ROUTE_ID_TO_SHORT)
    return {
        "stations_count": len(STATION_INDEX),
        "bbox": list(SYD_BBOX) if SYD_BBOX else None,
        "feeds": URLS_VP,
        "total_trains": len(trains),
        "routes_found": sorted(by_route.keys()),
        "routes_missing": [r for r, v in route_debug.items() if v is None],
        "routes_resolved": route_debug,
    }

@app.get("/trains")
def trains():
    """Debug endpoint to see raw train data"""
    feed, age = fetch_vp_cached()
    trains, _, _ = collect_trains(feed, ROUTE_ID_TO_SHORT)

    # rt.VehiclePosition.VehicleStopStatus:
    # INCOMING_AT=0, STOPPED_AT=1, IN_TRANSIT_TO=2
    def status_name(st: int) -> str:
        return {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}.get(st, "?")

    out = []
    # Also collect raw route_ids from feed for debugging
    raw_routes = set()
    
    for e in feed.entity[:50]:  # Sample first 50
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        rid = v.trip.route_id if v.HasField("trip") else ""
        if rid:
            raw_routes.add(rid)
    
    for lat, lon, rs, sid, st in trains[:50]:
        out.append({
            "route": rs,
            "lat": lat,
            "lon": lon,
            "stop_id": sid,
            "status": status_name(st),
            "parent_station": CHILD_TO_PARENT.get(sid, sid) if sid else None,
        })

    return {
        "feed_age_seconds": age, 
        "total_trains": len(trains),
        "trains_with_routes": len([t for t in trains if t[2] is not None]),
        "trains_without_routes": len([t for t in trains if t[2] is None]),
        "sample_raw_route_ids": sorted(list(raw_routes)[:20]),
        "sample_trains": out
    }

@app.get("/board_settings")
def board_settings_api(board_id: str = Query(...)):
    board_id = board_id.strip()
    if not board_id:
        return JSONResponse({"error": "missing board_id"}, status_code=400)
    try:
        cfg = _get_or_create_board(board_id)
    except ValueError:
        return JSONResponse({"error": "invalid board_id"}, status_code=400)
    return JSONResponse(cfg)

@app.get("/stations")
def stations():
    # Sorted by name for stable UI
    return JSONResponse([STATION_INDEX[k] for k in sorted(STATION_INDEX.keys())])

@app.get("/states")
def states(
    only: T.Optional[str] = Query(None, description="Comma-separated station names"),
    board_id: T.Optional[str] = Query(None, description="Board ID (5-char code)")
):
    """
    Returns status for each station.
    If board_id is provided, we require that board to exist in boards.json.
    """
    if board_id is not None:
        board_id = board_id.strip()
        if not board_id or board_id not in BOARD_SETTINGS:
            return JSONResponse({"error": "unknown board_id"}, status_code=403)
        # You *could* read mode here and e.g. short-circuit if mode=="animation".

    subset = STATION_INDEX

    if only:
        wanted = [x.strip() for x in only.split(",") if x.strip()]
        subset = {n: STATION_INDEX[n] for n in wanted if n in STATION_INDEX}
        if not subset:
            return JSONResponse({})

    feed, _ = fetch_vp_cached()
    trains, _by_route, _ = collect_trains(feed, ROUTE_ID_TO_SHORT)

    if MB_VERBOSE:
        vlog(f"processing trains={len(trains)} stations={len(subset)}")

    if not trains:
        return JSONResponse({k: 0 for k in subset.keys()})

    VP_INCOMING = rt.VehiclePosition.INCOMING_AT
    VP_STOPPED  = rt.VehiclePosition.STOPPED_AT

    out: dict[str, int] = {}
    matches = 0
    dist_fb = 0

    for name, meta in subset.items():
        pid = meta["stop_id"]
        slat, slon = meta["lat"], meta["lon"]
        # THIS IS THE KEY FIX: Each station has its specific route_short
        station_route = (meta.get("route_short") or "").upper()

        best = 0
        best_d = None

        # CRITICAL: Only look at trains that match THIS station's route
        for (tlat, tlon, train_route, child_sid, st) in trains:
            # SKIP trains that don't match this station's route
            # Allow None train_route to fall through to distance-only matching
            if train_route is not None and train_route != station_route:
                continue

            # VP match first (unless forced distance)
            if not MB_FORCE_DISTANCE and child_sid:
                parent = CHILD_TO_PARENT.get(child_sid) or (child_sid if is_num_parent(child_sid) else None)
                if parent == pid:
                    if st == VP_STOPPED:
                        best = 3
                        matches += 1
                        break
                    elif st == VP_INCOMING:
                        best = max(best, 2)
                        matches += 1

            # Distance fallback (only for trains on the correct route or unknown route)
            if best < 3 and tlat is not None and tlon is not None:
                d = hav_m(slat, slon, tlat, tlon)
                if best_d is None or d < best_d:
                    best_d = d

        if best == 0 and best_d is not None:
            best = status_from_dist(best_d)
            if best > 0:
                dist_fb += 1

        out[name] = best

    if MB_VERBOSE:
        act = sum(1 for v in out.values() if v > 0)
        vlog(f"result active={act}/{len(subset)} matches={matches} distance_fb={dist_fb}")

    return JSONResponse(out)

# ---------------- run (dev) ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=os.getenv("RELOAD", "0") == "1")
