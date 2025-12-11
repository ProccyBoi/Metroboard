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
API_TOKEN = os.environ["TFNSW_API_TOKEN"]

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
DEFAULT_BOARD_IDS = [
    bid.strip() for bid in (os.getenv("DEFAULT_BOARD_IDS") or "AB12C").split(",")
    if bid.strip()
]

# board_id -> settings dict
BOARD_SETTINGS: dict[str, dict] = {}

# Supported LED animation names (mirrors ESP demo options)
ANIMATION_CHOICES: list[str] = [
    "pulse",
    "rainbow",
    "theater",
    "sparkle",
    "solid",
]

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
        seeded = {_id: _default_board_settings(_id) for _id in DEFAULT_BOARD_IDS}
        if seeded:
            try:
                with open(BOARD_SETTINGS_PATH, "w", encoding="utf-8") as f:
                    json.dump(seeded, f, indent=2, sort_keys=True)
            except Exception as e:
                vlog("WARN: failed to create boards.json:", e)
        return seeded
    try:
        with open(BOARD_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
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
        "brightness": {"live": 160, "animation": 160},
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
    cfg = BOARD_SETTINGS[board_id]
    _normalize_board_settings(cfg)
    return cfg


def _normalize_board_settings(cfg: dict) -> None:
    """Ensure persisted boards always have expected keys and shapes."""

    def _norm_brightness(v: T.Any, fallback: int = 160) -> dict[str, int]:
        if isinstance(v, dict):
            live = int(v.get("live", v.get("default", fallback)))
            anim = int(v.get("animation", live))
        else:
            try:
                live = anim = int(v)
            except Exception:
                live = anim = fallback
        return {
            "live": max(0, min(255, live)),
            "animation": max(0, min(255, anim)),
        }

    cfg.setdefault("board_id", "")
    cfg["brightness"] = _norm_brightness(cfg.get("brightness", 160))
    cfg.setdefault("mode", "live")
    nm = cfg.get("night_mode") or {}
    cfg["night_mode"] = {
        "enabled": bool(nm.get("enabled")),
        "start": nm.get("start", "23:00"),
        "end": nm.get("end", "06:00"),
        "action": nm.get("action", "off"),
        "dim_level": max(0, min(255, int(nm.get("dim_level", 20))))
    }

    an = cfg.get("animation") or {}
    name = (an.get("name") or "pulse").lower()
    if name not in ANIMATION_CHOICES:
        name = ANIMATION_CHOICES[0]
    color = [int(c) for c in (an.get("color") or [255, 80, 0])][:3]
    while len(color) < 3:
        color.append(0)
    cfg["animation"] = {
        "name": name,
        "speed": max(1, int(an.get("speed", 120))),
        "color": color,
    }


def _get_brightness_for_mode(cfg: dict, mode: str) -> int:
    b = cfg.get("brightness", {})
    if isinstance(b, dict):
        val = b.get(mode, b.get("live", 160))
    else:
        try:
            val = int(b)
        except Exception:
            val = 160
    return max(0, min(255, int(val)))


def _parse_minutes(hhmm: str) -> T.Optional[int]:
    try:
        hh, mm = hhmm.split(":", 1)
        return (int(hh) % 24) * 60 + (int(mm) % 60)
    except Exception:
        return None


def _night_mode_active(cfg: dict, *, now_minutes: T.Optional[int] = None) -> bool:
    nm = cfg.get("night_mode") or {}
    if not nm.get("enabled"):
        return False
    start = _parse_minutes(nm.get("start", ""))
    end = _parse_minutes(nm.get("end", ""))
    if start is None or end is None:
        return False
    if now_minutes is None:
        t = time.localtime()
        now_minutes = t.tm_hour * 60 + t.tm_min
    if start <= end:
        return start <= now_minutes < end
    return now_minutes >= start or now_minutes < end


def _effective_brightness(cfg: dict, *, now_minutes: T.Optional[int] = None) -> tuple[int, bool, str]:
    mode = (cfg.get("mode") or "live").lower()
    base_b = _get_brightness_for_mode(cfg, mode)
    nm = cfg.get("night_mode") or {}
    nm_active = _night_mode_active(cfg, now_minutes=now_minutes)

    if nm_active:
        action = (nm.get("action") or "off").lower()
        if action == "off":
            return 0, True, "off"
        dim_level = max(0, min(255, int(nm.get("dim_level", base_b))))
        return min(base_b, dim_level), True, mode

    return base_b, False, mode

EXCLUDE_SUB = ("wharf", "light rail", "b-line", "interchange", "motorway", " m2")
SHORT_RE = re.compile(r"\b([TM]\d{1,2})\b", re.I)
LINE_CODE_RE = re.compile(r"^(T\d{1,2}|M\d|CCN|SCO|BMT|HUN)$", re.I)

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


def is_core_line(rs: str) -> bool:
    """Return True for supported passenger rail/metro line short codes."""

    return bool(LINE_CODE_RE.match((rs or "").strip()))

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


def load_trip_to_route(trips_path: str = "trips.txt") -> dict[str, str]:
    mapping: dict[str, str] = {}
    try:
        with open(trips_path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                tid = (r.get("trip_id") or "").strip()
                rid = (r.get("route_id") or "").strip()
                if tid and rid:
                    mapping[tid] = rid
    except Exception as e:
        vlog(f"WARN: failed to load trips map: {e}")
    vlog(f"trip→route map: {len(mapping)}")
    return mapping

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
            if not is_core_line(rshort):
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
            if not is_core_line(rshort):
                continue
            if route_short_allow and rshort not in route_short_allow:
                continue
            p = parent_of((r.get("stop_id") or "").strip())
            if p and is_num_parent(p):
                pairs.add((p, rshort))

    if pairs:
        vlog(f"pairs strict: {len(pairs)}")
        return pairs

    # deterministic fallback from curated station_line_pairs.csv (avoids line fan-out)
    pairs_csv = os.getenv("STATION_LINE_PAIRS_PATH", "station_line_pairs.csv")
    if os.path.exists(pairs_csv):
        with open(pairs_csv, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                pid = (row.get("parent_stop_id") or "").strip()
                rs = (row.get("route_short") or "").strip().upper()
                if not (pid and rs):
                    continue
                if route_short_allow and rs not in route_short_allow:
                    continue
                if allowed_parents_for_fallback and pid not in allowed_parents_for_fallback:
                    continue
                if not is_core_line(rs):
                    continue
                pairs.add((pid, rs))
        if pairs:
            vlog(f"pairs from CSV: {len(pairs)}")
            return pairs

    # permissive fallback
    if os.getenv("MB_PERMISSIVE_PAIRS", "0") != "0":
        known = {rs for rs in _known_tm(route_id_to_short) if is_core_line(rs)}
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


def station_routes_lookup(station_index: dict[str, dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for meta in station_index.values():
        base = (meta.get("base_name") or "").lower()
        rs = (meta.get("route_short") or "").upper()
        if base and rs:
            out.setdefault(base, set()).add(rs)
    return out

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


def _route_from_label(label: str, station_routes_by_name: dict[str, set[str]]) -> T.Optional[str]:
    label = (label or "").strip()
    if not label:
        return None

    label = re.sub(r"^\d{1,2}:\d{2}\s+", "", label)
    parts = re.split(r"\s+to\s+", label, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None

    origin_raw, dest_raw = (clean_name(p) for p in parts)
    origin = origin_raw.lower()
    dest = dest_raw.lower()

    routes_o = station_routes_by_name.get(origin, set())
    routes_d = station_routes_by_name.get(dest, set())

    if routes_o and routes_d:
        common = routes_o & routes_d
        if common:
            return sorted(common)[0]

    for routes in (routes_o, routes_d):
        if len(routes) == 1:
            return next(iter(routes))

    return None


PREFIX_ROUTE_FALLBACK = {
    "APS": "T8",   # Airport & South
    "IWL": "T2",   # Inner West & Leppington
    "CMB": "T5",   # Cumberland
    "WST": "T1",   # Western
    "NSN": "T9",   # Northern (T9)
    "NTH": "T1",   # North Shore
    "ESI": "T4",   # Eastern Suburbs & Illawarra
    "BMT": "BMT",  # Blue Mountains
    "CCN": "CCN",  # Central Coast & Newcastle
    "SCO": "SCO",  # South Coast
}


def infer_vehicle_route(
    v: rt.VehiclePosition,
    route_id_to_short: dict[str, str],
    station_routes_by_name: dict[str, set[str]],
    trip_to_route: dict[str, str],
) -> T.Optional[str]:
    rid = v.trip.route_id if v.HasField("trip") else ""
    tripid = v.trip.trip_id if v.HasField("trip") else ""
    rid = rid or trip_to_route.get(tripid, "")
    label = getattr(v.vehicle, "label", "") if v.HasField("vehicle") else ""

    rs = _route_from_label(label, station_routes_by_name)
    if not rs:
        rs = (route_id_to_short.get(rid, "") or "").upper()

    if not rs and rid:
        prefix = rid.split("_", 1)[0].upper()
        rs = PREFIX_ROUTE_FALLBACK.get(prefix, "")

    if not rs:
        for src in (rid, tripid):
            if not src:
                continue
            m = re.search(r"\b([TM]\d{1,2})\b", src, re.I)
            if m:
                rs = m.group(1).upper()
                break

    return rs or None


def collect_trains(
    feed: rt.FeedMessage,
    route_id_to_short: dict[str, str],
    station_routes_by_name: dict[str, set[str]],
    trip_to_route: dict[str, str],
) -> list[dict[str, T.Any]]:
    trains: list[dict[str, T.Any]] = []

    for e in feed.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        route = infer_vehicle_route(v, route_id_to_short, station_routes_by_name, trip_to_route)

        if route and ROUTE_SHORT_ALLOW and route not in ROUTE_SHORT_ALLOW:
            continue

        sid = getattr(v, "stop_id", "")
        parent_stop = None
        if sid:
            parent_stop = CHILD_TO_PARENT.get(sid) or (sid if is_num_parent(sid) else None)

        trains.append(
            {
                "lat": getattr(v.position, "latitude", None) if v.HasField("position") else None,
                "lon": getattr(v.position, "longitude", None) if v.HasField("position") else None,
                "route": route,
                "stop_id": sid,
                "parent_stop": parent_stop,
                "status": int(getattr(v, "current_status", rt.VehiclePosition.IN_TRANSIT_TO)),
                "label": getattr(v.vehicle, "label", "") if v.HasField("vehicle") else "",
            }
        )

    vlog(f"collect_trains kept={len(trains)}")
    return trains


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
STATION_ROUTES_BY_NAME: dict[str, set[str]] = {}
TRIP_TO_ROUTE: dict[str, str] = {}
STATIONS_BY_STOP_ROUTE: dict[tuple[str, str], list[str]] = {}

@app.on_event("startup")
def _startup():
    global ROUTE_ID_TO_SHORT, STATION_INDEX, CHILD_TO_PARENT, BOARD_SETTINGS, STATION_ROUTES_BY_NAME, TRIP_TO_ROUTE, STATIONS_BY_STOP_ROUTE

    BOARD_SETTINGS = _load_board_settings()
    for cfg in BOARD_SETTINGS.values():
        _normalize_board_settings(cfg)

    ROUTE_ID_TO_SHORT = load_route_shortnames("routes.txt")
    TRIP_TO_ROUTE = load_trip_to_route("trips.txt")

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

    STATION_ROUTES_BY_NAME = station_routes_lookup(STATION_INDEX)

    STATIONS_BY_STOP_ROUTE = {}
    for name, meta in STATION_INDEX.items():
        key = (meta["stop_id"], (meta.get("route_short") or "").upper())
        STATIONS_BY_STOP_ROUTE.setdefault(key, []).append(name)

    CHILD_TO_PARENT = build_child_to_parent("stops.txt")

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

    b_live = _get_brightness_for_mode(cfg, "live")
    b_anim = _get_brightness_for_mode(cfg, "animation")
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

    anim_options = "".join(
        f"<option value=\"{opt}\" {'selected' if an_name==opt else ''}>{opt.title()}</option>"
        for opt in ANIMATION_CHOICES
    )

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
          <label>Live mode brightness:<br>
            <input type="range" min="0" max="255" name="brightness_live" value="{b_live}" />
          </label><br>
          <label>Animation brightness:<br>
            <input type="range" min="0" max="255" name="brightness_animation" value="{b_anim}" />
          </label><br>
          <small>Both modes respect the night mode settings below.</small>

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
            <select name="an_name">{anim_options}</select>
          </label><br>
          <label>Speed:
            <input type="number" name="an_speed" min="1" max="255" value="{an_speed}">
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
    brightness_live: int = Form(160),
    brightness_animation: int = Form(160),
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
    brightness_live = max(0, min(255, brightness_live))
    brightness_animation = max(0, min(255, brightness_animation))
    nm_dim = max(0, min(255, nm_dim))

    def _parse_hex_color(s: str) -> list[int]:
        s = s.lstrip("#")
        if len(s) == 3: s = "".join(c*2 for c in s)
        if len(s) != 6: return [255, 80, 0]
        return [int(s[i:i+2], 16) for i in (0, 2, 4)]

    cfg["mode"] = mode
    cfg["brightness"] = {"live": brightness_live, "animation": brightness_animation}
    cfg["night_mode"] = {
        "enabled": bool(nm_enabled),
        "start": nm_start,
        "end": nm_end,
        "action": nm_action,
        "dim_level": nm_dim,
    }
    cfg["animation"] = {
        "name": an_name if an_name in ANIMATION_CHOICES else ANIMATION_CHOICES[0],
        "speed": max(1, an_speed),
        "color": _parse_hex_color(an_color),
    }

    _normalize_board_settings(cfg)

    BOARD_SETTINGS[board_id] = cfg
    _save_board_settings()

    return RedirectResponse(f"/board/{board_id}", status_code=303)

@app.get("/debug")
def debug():
    feed, _ = fetch_vp_cached()
    trains = collect_trains(feed, ROUTE_ID_TO_SHORT, STATION_ROUTES_BY_NAME, TRIP_TO_ROUTE)
    by_route: dict[str, int] = {}
    for t in trains:
        if t["route"]:
            by_route[t["route"]] = by_route.get(t["route"], 0) + 1
    return {
        "stations_count": len(STATION_INDEX),
        "bbox": list(SYD_BBOX) if SYD_BBOX else None,
        "feeds": URLS_VP,
        "total_trains": len(trains),
        "routes_found": sorted(by_route.keys()),
    }

@app.get("/trains")
def trains():
    """Debug endpoint to see raw train data"""
    feed, age = fetch_vp_cached()
    trains = collect_trains(feed, ROUTE_ID_TO_SHORT, STATION_ROUTES_BY_NAME, TRIP_TO_ROUTE)

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

    for train in trains[:50]:
        sid = train.get("stop_id")
        out.append({
            "route": train.get("route"),
            "lat": train.get("lat"),
            "lon": train.get("lon"),
            "stop_id": sid,
            "status": status_name(train.get("status", 2)),
            "parent_station": CHILD_TO_PARENT.get(sid, sid) if sid else None,
        })

    return {
        "feed_age_seconds": age,
        "total_trains": len(trains),
        "trains_with_routes": len([t for t in trains if t.get("route") is not None]),
        "trains_without_routes": len([t for t in trains if t.get("route") is None]),
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
    eff_b, night_active, eff_mode = _effective_brightness(cfg)
    resp = dict(cfg)
    resp["effective_brightness"] = eff_b
    resp["effective_mode"] = eff_mode
    resp["night_mode_active"] = night_active
    resp["animations_available"] = ANIMATION_CHOICES
    return JSONResponse(resp)

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
    trains = collect_trains(feed, ROUTE_ID_TO_SHORT, STATION_ROUTES_BY_NAME, TRIP_TO_ROUTE)

    if MB_VERBOSE:
        vlog(f"processing trains={len(trains)} stations={len(subset)}")

    out: dict[str, int] = {k: 0 for k in subset.keys()}

    stop_route_subset: dict[tuple[str, str], list[str]] = {}
    if subset is STATION_INDEX:
        for key, names in STATIONS_BY_STOP_ROUTE.items():
            stop_route_subset[key] = list(names)
    else:
        for key, names in STATIONS_BY_STOP_ROUTE.items():
            filtered = [n for n in names if n in subset]
            if filtered:
                stop_route_subset[key] = filtered

    for train in trains:
        route = train.get("route")
        parent = train.get("parent_stop")
        if not parent:
            continue

        # If the route is missing, try every station under this parent.
        target_keys: list[tuple[str, str]]
        if route:
            key = (parent, route)
            target_keys = [key] if key in stop_route_subset else []
        else:
            target_keys = [key for key in stop_route_subset if key[0] == parent]

        if not target_keys:
            continue

        for key in target_keys:
            for station_name in stop_route_subset.get(key, []):
                if out.get(station_name) == 3:
                    continue

                meta = subset[station_name]
                slat, slon = meta["lat"], meta["lon"]
                tlat, tlon = train.get("lat"), train.get("lon")
                near = False
                if tlat is not None and tlon is not None:
                    near = hav_m(slat, slon, tlat, tlon) <= 100

                if near or train.get("status") == rt.VehiclePosition.STOPPED_AT or train.get("stop_id"):
                    out[station_name] = 3

    return JSONResponse(out)

# ---------------- run (dev) ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=os.getenv("RELOAD", "0") == "1")
