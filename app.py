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
from datetime import datetime, time as dtime
import typing as T

import requests
from fastapi import FastAPI, Query, Request, Form
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from google.transit import gtfs_realtime_pb2 as rt
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - python <3.9 fallback
    ZoneInfo = None

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
BOARD_SETTINGS_VERSION: int = 0
BOARD_SETTINGS_VERSION: int = 0

# Caching/CORS/logging
FEED_CACHE_SEC = int(os.getenv("FEED_CACHE_SEC", "3"))
CORS_ALLOW_ORIGIN = os.getenv("CORS_ALLOW_ORIGIN")
MB_VERBOSE = os.getenv("MB_VERBOSE", "1") == "1"
MB_FORCE_DISTANCE = os.getenv("MB_FORCE_DISTANCE", "0") == "1"
MB_TIMEZONE = os.getenv("MB_TIMEZONE", "Australia/Sydney")
FIRMWARE_VERSION = os.getenv("MB_FIRMWARE_VERSION", "").strip()
FIRMWARE_URL = os.getenv("MB_FIRMWARE_URL", "").strip()
FIRMWARE_SHA256 = os.getenv("MB_FIRMWARE_SHA256", "").strip()

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

def _resolve_local_path(path: str) -> str:
    if not path:
        return path
    if os.path.isabs(path):
        return path
    if os.path.exists(path):
        return path
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, path)

def _load_board_settings() -> dict[str, dict]:
    settings_path = _resolve_local_path(BOARD_SETTINGS_PATH)
    if not os.path.exists(settings_path):
        vlog(f"WARN: boards.json not found at {settings_path}")
        return {}
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            normalized: dict[str, dict] = {}
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
                normalized_id = _normalize_board_id(bid or cfg.get("board_id"))
                if not normalized_id:
                    continue
                cfg["board_id"] = normalized_id
                normalized[normalized_id] = cfg
            vlog(f"boards.json loaded: {len(normalized)} boards from {settings_path}")
            return normalized
    except Exception as e:
        vlog("WARN: failed to load boards.json:", e)
    return {}

def _save_board_settings() -> None:
    global BOARD_SETTINGS_VERSION
    BOARD_SETTINGS_VERSION += 1
    settings_path = _resolve_local_path(BOARD_SETTINGS_PATH)
    try:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(BOARD_SETTINGS, f, indent=2, sort_keys=True)
    except Exception as e:
        vlog("WARN: failed to save boards.json:", e)

def _now_local() -> datetime:
    """Current time in configured timezone; falls back to UTC if TZ invalid/unavailable."""
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(MB_TIMEZONE))
        except Exception:
            pass
    return datetime.utcnow()

def _parse_hhmm(s: str) -> T.Optional[dtime]:
    try:
        return datetime.strptime(s, "%H:%M").time()
    except Exception:
        return None

def _night_mode_effect(brightness: int, nm: dict) -> tuple[int, bool]:
    """
    Returns (effective_brightness, active_flag) based on night-mode settings.
    If parsing fails or disabled, returns (brightness, False).
    """
    if not nm or not nm.get("enabled"):
        return brightness, False

    start = _parse_hhmm(nm.get("start", ""))
    end = _parse_hhmm(nm.get("end", ""))
    if not start or not end:
        return brightness, False

    now_local = _now_local().time()
    if start <= end:
        active = start <= now_local < end
    else:
        # Window crosses midnight
        active = now_local >= start or now_local < end

    if not active:
        return brightness, False

    action = (nm.get("action") or "").lower()
    dim_level = int(nm.get("dim_level", 0))
    dim_level = max(0, min(255, dim_level))

    if action == "off":
        return 0, True
    return dim_level, True

def _default_board_settings(board_id: str) -> dict:
    return {
        "board_id": board_id,
        "brightness": 16,
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

def _normalize_board_id(board_id: T.Optional[str]) -> str:
    return (board_id or "").strip().upper()

def _get_or_create_board(board_id: str) -> dict:
    board_id = _normalize_board_id(board_id)
    if not board_id:
        vlog("invalid board_id: empty")
        raise ValueError("empty board_id")
    if board_id not in BOARD_SETTINGS:
        vlog(f"invalid board_id: {board_id} not in BOARD_SETTINGS")
        raise ValueError("unknown board_id")
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

def extract_station_name_from_stop_id(stop_id: str) -> T.Optional[str]:
    """
    Extract station name from TfNSW location-based stop_ids.
    Examples:
      "Blacktown.Seven Hills 2 Loc" -> "Seven Hills"
      "NorthShore.Lindfield 3 Loc" -> "Lindfield"
      "Sydney.Circular Quay 1 Loc" -> "Circular Quay"
      "Glenfield.GFD Dn Ctn Exit Loc" -> None (code pattern)
      "Lidcombe.AY997/548 Loc" -> None (code pattern)
    """
    if not stop_id or "." not in stop_id:
        return None
    
    # Split on dot: "Area.StationInfo"
    parts = stop_id.split(".", 1)
    if len(parts) < 2:
        return None
    
    station_part = parts[1].strip()
    
    # Remove common suffixes and numeric indicators
    for suffix in [" Loc", " Entry", " Exit", " Dep", " Arr", " Dn", " Up", " Ctn"]:
        station_part = station_part.replace(suffix, "")
    
    # Remove trailing numbers (platform indicators): " 1", " 2", etc.
    station_part = re.sub(r'\s+\d+$', '', station_part)
    
    station_part = station_part.strip()
    
    # Skip if empty or too short
    if len(station_part) < 3:
        return None
    
    # Skip if it looks like a code (starts with 2-3 capital letters followed by numbers or special chars)
    if re.match(r'^[A-Z]{2,3}[\d/]', station_part):
        return None
    
    # Skip if it contains slashes with numbers (technical location codes)
    if '/' in station_part and any(c.isdigit() for c in station_part):
        return None
    
    # Clean up extra spaces
    station_part = " ".join(station_part.split())
    
    return station_part if station_part else None

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
        "IWL": ["T2", "T4"],     # Inner West (can be T2 or T4)
        "LPP": ["T2"],           # Leppington specific
        "T2": ["T2"],            # Direct T2 code
        "T3": ["T3"],            # Bankstown Line
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
        "SM": ["M1"], 
        "SMNW": ["M1"],          # Sydney Metro Northwest
        "M1": ["M1"],            # Metro M1
        "M": ["M1"],      # Catch generic "M" prefixes
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
                    # Multiple matches - use better heuristics
                    
                    # Priority 1: T8 keywords (Airport/South)
                    if any(x in rid.upper() for x in ["AIR", "SHL", "APT", "ESS", "AIRS"]) and "T8" in possible:
                        rs = "T8"
                    
                    # Priority 2: T9 keywords (North Shore express)
                    elif any(x in rid.upper() for x in ["NSN", "NTH"]) and "T9" in possible and "T1" in possible:
                        # Use hash of trip_id to distribute between T1 and T9
                        # This gives consistent assignment per trip without needing to track history
                        trip_hash = abs(hash(tripid or rid))
                        # Roughly 60% T1 (stopping), 40% T9 (express) to match real-world distribution
                        if trip_hash % 10 < 6:
                            rs = "T1"
                        else:
                            rs = "T9"
                    
                    # Priority 3: Use trip_id/route_id hash for consistent distribution
                    else:
                        sorted_possible = sorted(possible)
                        trip_hash = abs(hash(tripid or rid))
                        rs = sorted_possible[trip_hash % len(sorted_possible)]
                    
                    if MB_VERBOSE and len(possible) > 1:
                        vlog(f"Multi-route disambiguation {rid} at {parent}: {possible} -> {rs}")
                else:
                    # None of the candidates serve this station - station data might be wrong
                    # Fall back to hash-based selection from original candidates
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
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

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
STATION_NAME_TO_PARENT: dict[str, str] = {}

@app.on_event("startup")
def _startup():
    global ROUTE_ID_TO_SHORT, STATION_INDEX, CHILD_TO_PARENT, BOARD_SETTINGS, STATION_ROUTE_PAIRS, STATION_NAME_TO_PARENT

    BOARD_SETTINGS = _load_board_settings()

    ROUTE_ID_TO_SHORT = load_route_shortnames("routes.txt")
    STATION_ROUTE_PAIRS = load_station_route_pairs("station_line_pairs.csv")
    CHILD_TO_PARENT = build_child_to_parent("stops.txt")
    
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

    if STATION_ROUTE_PAIRS:
        allowed_parents |= set(STATION_ROUTE_PAIRS.keys())
        allowed_pairs = {
            (pid, rs) for pid, routes in STATION_ROUTE_PAIRS.items() for rs in routes
        }
        vlog(f"allowed_pairs from station_line_pairs: {len(allowed_pairs)} (stations={len(STATION_ROUTE_PAIRS)})")
    else:
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

    for key, meta in STATION_INDEX.items():
        base_name = meta.get("base_name")
        pid = meta.get("stop_id")
        if base_name and pid:
            # Store clean name without route suffix
            STATION_NAME_TO_PARENT[base_name] = pid
    
    vlog(f"station_name→parent: {len(STATION_NAME_TO_PARENT)}")
    
    if not STATION_INDEX:
        raise RuntimeError("No stations after filtering")
    vlog(f"stations loaded: {len(STATION_INDEX)}")

@app.get("/")
def root():
    return {"ok": True}

@app.get("/healthz")
def healthz():
    age = None if _FEED_TS == 0 else (time.time() - _FEED_TS)
    return {"status": "ok", "stations": len(STATION_INDEX), "trains_last_fetch_s": age}

@app.get("/firmware/latest")
def firmware_latest():
    if not FIRMWARE_VERSION or not FIRMWARE_URL or not FIRMWARE_SHA256:
        return JSONResponse({"error": "firmware metadata not configured"}, status_code=404)
    return {
        "version": FIRMWARE_VERSION,
        "url": FIRMWARE_URL,
        "sha256": FIRMWARE_SHA256,
    }

@app.get("/login", response_class=HTMLResponse)
def login_get(error: T.Optional[str] = Query(None)):
    error_message = ""
    if error == "missing":
        error_message = "Enter the 5-character board ID to continue."
    elif error == "not-found":
        error_message = "We couldn't find that board ID. Double-check the code and try again."
    error_html = ""
    if error_message:
        error_html = f'<div class="error" role="alert">{error_message}</div>'
    html = """
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8" />
        <title>Metroboard Login</title>
        <style>
          :root {
            color-scheme: light dark;
            --bg: #0b0e14;
            --panel: #131923;
            --border: #263042;
            --accent: #ff6b3d;
            --muted: #8a96a8;
            --text: #edf2f7;
            --shadow: 0 18px 40px rgba(4, 8, 16, 0.45);
            --line-blue: #2f80ed;
            --line-green: #27ae60;
            --line-red: #eb5757;
            --line-yellow: #f2c94c;
            --line-purple: #9b51e0;
          }
          * { box-sizing: border-box; }
          body {
            margin: 0;
            font-family: "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
            background: radial-gradient(circle at top, #141b2b 0%, var(--bg) 55%);
            color: var(--text);
            min-height: 100vh;
          }
          .page {
            max-width: 880px;
            margin: 0 auto;
            padding: 32px 20px 60px;
          }
          .hero {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 24px;
            padding: 28px 30px;
            border-radius: 20px;
            background: linear-gradient(135deg, rgba(255,107,61,0.16), rgba(19,25,35,0.98));
            border: 1px solid rgba(255,255,255,0.06);
            box-shadow: var(--shadow);
          }
          .hero__title {
            font-size: 28px;
            font-weight: 700;
            letter-spacing: 0.02em;
          }
          .hero__subtitle { color: var(--muted); margin-top: 4px; }
          .hero__badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-size: 12px;
            padding: 6px 10px;
            border-radius: 999px;
            background: rgba(255,255,255,0.08);
            color: var(--text);
            margin-top: 12px;
          }
          .metro-strip {
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 8px;
            margin: 22px 0;
          }
          .metro-strip span {
            height: 6px;
            border-radius: 999px;
            display: block;
          }
          .line-blue { background: var(--line-blue); }
          .line-green { background: var(--line-green); }
          .line-red { background: var(--line-red); }
          .line-yellow { background: var(--line-yellow); }
          .line-purple { background: var(--line-purple); }
          .card {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 24px;
            box-shadow: var(--shadow);
          }
          label.field {
            display: flex;
            flex-direction: column;
            gap: 8px;
            color: var(--muted);
            font-size: 14px;
          }
          label.field input {
            background: rgba(255,255,255,0.04);
            color: var(--text);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 12px 14px;
            font-size: 16px;
            outline: none;
            color-scheme: dark;
          }
          .helper {
            margin: 8px 0 0;
            font-size: 13px;
            color: var(--muted);
          }
          .error {
            margin-top: 14px;
            padding: 12px 14px;
            border-radius: 12px;
            border: 1px solid rgba(235, 87, 87, 0.55);
            background: rgba(235, 87, 87, 0.12);
            color: #f5c2c2;
            font-size: 14px;
          }
          button.primary {
            margin-top: 18px;
            width: 100%;
            background: var(--accent);
            color: #0d0f14;
            border: none;
            border-radius: 12px;
            padding: 12px 18px;
            font-weight: 700;
            font-size: 15px;
            cursor: pointer;
            box-shadow: 0 12px 30px rgba(255,107,61,0.35);
          }
          button.primary:hover {
            transform: translateY(-1px);
            box-shadow: 0 16px 32px rgba(255,107,61,0.4);
          }
          @media (max-width: 720px) {
            .hero { flex-direction: column; align-items: flex-start; }
          }
        </style>
      </head>
      <body>
        <main class="page">
          <div class="hero">
            <div>
              <div class="hero__title">Metroboard</div>
              <div class="hero__subtitle">Sign in to manage your board.</div>
              <div class="hero__badge">Train + Metro network · Live sync</div>
            </div>
          </div>
          <div class="metro-strip" aria-hidden="true">
            <span class="line-blue"></span>
            <span class="line-green"></span>
            <span class="line-red"></span>
            <span class="line-yellow"></span>
            <span class="line-purple"></span>
          </div>

          <section class="card">
            <form method="post" action="/login">
              <label class="field">Board ID
                <input type="text" name="board_id" minlength="5" maxlength="5" required placeholder="e.g. PZ23T" />
              </label>
              <p class="helper">Use the 5 character code printed on your card.</p>
              __ERROR_HTML__
              <button class="primary" type="submit">Open board</button>
            </form>
          </section>
        </main>
      </body>
    </html>
    """
    return html.replace("__ERROR_HTML__", error_html)

@app.post("/login")
async def login_post(board_id: str = Form(...)):
    board_id = _normalize_board_id(board_id)
    if not board_id:
        return RedirectResponse("/login?error=missing", status_code=303)
    if board_id not in BOARD_SETTINGS:
        vlog(f"login rejected: board_id {board_id} not found in BOARD_SETTINGS")
        return RedirectResponse("/login?error=not-found", status_code=303)
    return RedirectResponse(f"/board/{board_id}", status_code=303)


@app.get("/board/{board_id}", response_class=HTMLResponse)
def board_settings_page(board_id: str):
    board_id = _normalize_board_id(board_id)
    try:
        cfg = _get_or_create_board(board_id)
    except ValueError:
        return HTMLResponse("Invalid board_id", status_code=400)

    b = min(150, int(cfg.get("brightness", 16)))
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
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8" />
        <title>Metroboard {board_id} Settings</title>
        <style>
          :root {{
            color-scheme: light dark;
            --bg: #0b0e14;
            --panel: #131923;
            --border: #263042;
            --accent: #ff6b3d;
            --muted: #8a96a8;
            --text: #edf2f7;
            --shadow: 0 18px 40px rgba(4, 8, 16, 0.45);
            --line-blue: #2f80ed;
            --line-green: #27ae60;
            --line-red: #eb5757;
            --line-yellow: #f2c94c;
            --line-purple: #9b51e0;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
            background:
              radial-gradient(circle at top, rgba(47,128,237,0.18), transparent 50%),
              radial-gradient(circle at 20% 20%, rgba(155,81,224,0.16), transparent 55%),
              linear-gradient(145deg, #0a0e16 0%, #131a29 100%);
            color: var(--text);
            min-height: 100vh;
          }}
          body::before {{
            content: "";
            position: fixed;
            inset: 0;
            pointer-events: none;
            background-image:
              linear-gradient(rgba(255,255,255,0.04) 1px, transparent 1px),
              linear-gradient(90deg, rgba(255,255,255,0.04) 1px, transparent 1px);
            background-size: 24px 24px;
            opacity: 0.4;
          }}
          a {{ color: var(--accent); }}
          main.page {{
            max-width: 900px;
            margin: 0 auto;
            padding: 32px 20px 60px;
          }}
          .hero {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 16px;
          }}
          .hero__title {{
            font-size: 32px;
            font-weight: 700;
            letter-spacing: 0.4px;
          }}
          .hero__subtitle {{ color: var(--muted); margin-top: 4px; }}
          .hero__hint {{ margin: 8px 0 0; color: var(--muted); }}
          .hero__badge {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin-top: 10px;
            padding: 6px 12px;
            border-radius: 999px;
            background: rgba(47,128,237,0.2);
            border: 1px solid rgba(47,128,237,0.35);
            color: #b9d7ff;
            font-size: 13px;
            font-weight: 600;
          }}
          .metro-strip {{
            display: flex;
            gap: 8px;
            margin: 18px 0 6px;
          }}
          .metro-strip span {{
            flex: 1;
            height: 6px;
            border-radius: 999px;
            box-shadow: 0 0 12px rgba(255,255,255,0.12);
          }}
          .metro-strip .line-blue {{ background: linear-gradient(90deg, rgba(47,128,237,0.6), var(--line-blue)); }}
          .metro-strip .line-green {{ background: linear-gradient(90deg, rgba(39,174,96,0.5), var(--line-green)); }}
          .metro-strip .line-red {{ background: linear-gradient(90deg, rgba(235,87,87,0.5), var(--line-red)); }}
          .metro-strip .line-yellow {{ background: linear-gradient(90deg, rgba(242,201,76,0.5), var(--line-yellow)); }}
          .metro-strip .line-purple {{ background: linear-gradient(90deg, rgba(155,81,224,0.5), var(--line-purple)); }}
          .card {{
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 20px;
            margin-top: 18px;
            box-shadow: var(--shadow);
          }}
          .section-head {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 8px;
          }}
          .section-head h3 {{ margin: 0; font-size: 18px; }}
          .section-head .tag {{
            background: rgba(255,255,255,0.06);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 6px 10px;
            font-size: 13px;
            color: var(--muted);
          }}
          .row {{ display: flex; flex-wrap: wrap; gap: 14px; margin-top: 10px; }}
          label.field {{
            display: flex;
            flex-direction: column;
            gap: 6px;
            min-width: 220px;
            flex: 1 1 220px;
            color: var(--muted);
            font-size: 14px;
          }}
          label.field input,
          label.field select {{
            background: rgba(255,255,255,0.04);
            color: var(--text);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 10px 12px;
            font-size: 15px;
            outline: none;
            color-scheme: dark;
          }}
          label.field select option {{
            background: #0f1420;
            color: var(--text);
          }}
          .color-picker {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 8px 12px;
            border-radius: 12px;
            border: 1px solid var(--border);
            background: rgba(255,255,255,0.04);
            cursor: pointer;
          }}
          .color-picker__input {{
            width: 44px;
            height: 36px;
            padding: 0;
            border: none;
            background: transparent;
            cursor: pointer;
          }}
          .color-picker__input::-webkit-color-swatch-wrapper {{
            padding: 0;
          }}
          .color-picker__input::-webkit-color-swatch {{
            border: 1px solid rgba(255,255,255,0.18);
            border-radius: 10px;
          }}
          .color-picker__value {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-size: 14px;
            color: var(--text);
            font-weight: 600;
          }}
          .color-picker__swatch {{
            width: 18px;
            height: 18px;
            border-radius: 6px;
            border: 1px solid rgba(255,255,255,0.2);
            box-shadow: 0 0 8px rgba(0,0,0,0.4);
          }}
          .color-picker__cta {{
            font-size: 12px;
            font-weight: 600;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.08em;
          }}
          .mode-options {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 10px;
            margin-top: 12px;
          }}
          .option {{
            border: 1px solid var(--border);
            padding: 12px 14px;
            border-radius: 12px;
            display: flex;
            gap: 10px;
            align-items: center;
            background: rgba(255,255,255,0.02);
          }}
          .pill {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: rgba(255,107,61,0.14);
            color: var(--accent);
            padding: 6px 10px;
            border-radius: 999px;
            font-weight: 600;
            font-size: 13px;
          }}
          .muted {{ color: var(--muted); font-size: 14px; margin: 0; }}
          .hidden {{ display: none; }}
          .slider-row {{
            display: flex;
            align-items: center;
            gap: 12px;
            margin-top: 12px;
          }}
          input[type="range"] {{
            flex: 1;
            accent-color: var(--accent);
          }}
          .note {{ color: var(--muted); font-size: 13px; margin-top: 8px; }}
          button.primary {{
            margin-top: 18px;
            background: var(--accent);
            color: #0d0f14;
            border: none;
            border-radius: 12px;
            padding: 12px 18px;
            font-weight: 700;
            cursor: pointer;
            box-shadow: 0 12px 30px rgba(255,107,61,0.35);
          }}
          button.primary:hover {{
            transform: translateY(-1px);
            box-shadow: 0 16px 32px rgba(255,107,61,0.4);
          }}
          .two-col {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }}
          .status {{ color: #a8ffce; font-weight: 600; font-size: 14px; }}
          .api-card code {{ background: rgba(255,255,255,0.06); padding: 3px 6px; border-radius: 6px; }}
          @media (max-width: 640px) {{
            .hero {{ flex-direction: column; }}
          }}
        </style>
      </head>
      <body>
        <main class="page">
          <div class="hero">
            <div>
              <div class="hero__title">Metroboard</div>
              <div class="hero__subtitle">Board <code>{board_id}</code></div>
              <p class="hero__hint">Change how your lights behave and save updates to the board.</p>
              <div class="hero__badge">Train + Metro network · Live sync</div>
            </div>
            <div class="hero__links"><a href="/login">Switch board</a></div>
          </div>
          <div class="metro-strip" aria-hidden="true">
            <span class="line-blue"></span>
            <span class="line-green"></span>
            <span class="line-red"></span>
            <span class="line-yellow"></span>
            <span class="line-purple"></span>
          </div>

          <form id="settingsForm" method="post" action="/board/{board_id}">
            <section class="card">
              <div class="section-head">
                <h3>Display Mode</h3>
                <span class="tag">Live vs Animation</span>
              </div>
              <div class="mode-options">
                <label class="option">
                  <input type="radio" name="mode" value="live" {'checked' if mode=='live' else ''} />
                  <div>
                    <div><strong>Live trains</strong></div>
                    <p class="muted">Use the network feed to light up stations.</p>
                  </div>
                </label>
                <label class="option">
                  <input type="radio" name="mode" value="animation" {'checked' if mode=='animation' else ''} />
                  <div>
                    <div><strong>Animation</strong></div>
                    <p class="muted">Ignore live trains and play the configured effect.</p>
                  </div>
                </label>
              </div>
            </section>

            <section class="card">
              <div class="section-head">
                <h3>Brightness</h3>
                <span class="tag">0–150</span>
              </div>
              <div class="slider-row">
                <input id="brightness" type="range" min="0" max="150" name="brightness" value="{b}" />
                <div class="pill" id="brightnessValue">{b}</div>
              </div>
              <p class="note">Applies to the entire board; night mode can override this later.</p>
            </section>

            <section class="card">
              <div class="section-head">
                <h3>Night Mode</h3>
                <span class="tag">Schedule</span>
              </div>
              <label class="option" style="margin-top: 6px;">
                <input id="nm_enabled" type="checkbox" name="nm_enabled" {nm_enabled} />
                <div>
                  <div><strong>Enable night mode</strong></div>
                  <p class="muted" id="nightStatus">11:00 PM → 06:00 AM · Turn off</p>
                </div>
              </label>
              <div class="two-col" style="margin-top: 12px;">
                <label class="field">Start time (e.g., 11:00 PM)
                  <input data-night-field type="time" name="nm_start" value="{nm_start}" />
                </label>
                <label class="field">End time (e.g., 06:00 AM)
                  <input data-night-field type="time" name="nm_end" value="{nm_end}" />
                </label>
              </div>
              <div class="two-col" style="margin-top: 10px;">
                <label class="field">Action
                  <select data-night-field name="nm_action" id="nm_action">
                    <option value="off" {"selected" if nm_action=="off" else ""}>Turn off</option>
                    <option value="dim" {"selected" if nm_action=="dim" else ""}>Dim</option>
                  </select>
                </label>
                <label class="field">Night brightness
                  <input data-night-field type="number" name="nm_dim" min="0" max="255" value="{nm_dim}" />
                </label>
              </div>
            </section>

            <section class="card">
              <div class="section-head">
                <h3>Animation</h3>
                <span class="tag" id="animationTag">Mode: {an_name.title()}</span>
              </div>
              <div class="row">
                <label class="field">Name
                  <select name="an_name" id="an_name">
                    <option value="pulse" {"selected" if an_name=="pulse" else ""}>Pulse</option>
                    <option value="bounce" {"selected" if an_name=="bounce" else ""}>Bounce</option>
                    <option value="rainbow" {"selected" if an_name=="rainbow" else ""}>Rainbow</option>
                  </select>
                </label>
                <label class="field">Speed
                  <input type="number" name="an_speed" value="{an_speed}" />
                </label>
                <label class="field" id="pulseColorField">Pulse colour
                  <div class="color-picker">
                    <input class="color-picker__input" type="color" id="an_color" name="an_color" value="{an_color_hex}" />
                    <span class="color-picker__value">
                      <span class="color-picker__swatch" id="pulseColorSwatch" style="background: {an_color_hex};"></span>
                      <span id="pulseColorValue">{an_color_hex}</span>
                    </span>
                    <span class="color-picker__cta">Change</span>
                  </div>
                </label>
              </div>
              <p class="note" id="pulseColorNote">Pulse is the only animation that uses the custom colour.</p>
              <p class="note">Animation settings are used when the board is in Animation mode.</p>
            </section>

            <button class="primary" type="submit">Save settings</button>
          </form>
        </main>

        <script>
          (function() {{
            const brightness = document.getElementById("brightness");
            const brightnessValue = document.getElementById("brightnessValue");
            const nmToggle = document.getElementById("nm_enabled");
            const nightFields = document.querySelectorAll("[data-night-field]");
            const nightStatus = document.getElementById("nightStatus");
            const nmAction = document.getElementById("nm_action");
            const startEl = document.querySelector('input[name="nm_start"]');
            const endEl = document.querySelector('input[name="nm_end"]');
            const anName = document.getElementById("an_name");
            const pulseColorField = document.getElementById("pulseColorField");
            const pulseColorNote = document.getElementById("pulseColorNote");
            const pulseColorInput = document.getElementById("an_color");
            const pulseColorValue = document.getElementById("pulseColorValue");
            const pulseColorSwatch = document.getElementById("pulseColorSwatch");

            const syncBrightness = () => brightnessValue.textContent = brightness.value;
            brightness.addEventListener("input", syncBrightness);
            syncBrightness();

            const formatTime = (v) => {{
              if (!v || !v.includes(":")) return v || "—";
              const [h, m] = v.split(":");
              const hr = parseInt(h, 10);
              const suffix = hr >= 12 ? "PM" : "AM";
              const hour12 = ((hr + 11) % 12) + 1;
              return `${{hour12}}:${{m}} ${{suffix}}`;
            }};

            const syncNight = () => {{
              const on = nmToggle.checked;
              nightFields.forEach(el => el.disabled = !on);
              const action = nmAction.value === "off" ? "Turn off" : "Dim to " + (document.querySelector('input[name="nm_dim"]').value || "20");
              nightStatus.textContent = on
                ? `${{formatTime(startEl.value)}} → ${{formatTime(endEl.value)}} · ${{action}}`
                : "Disabled";
            }};

            const syncPulseColorValue = () => {{
              if (!pulseColorInput) return;
              const value = (pulseColorInput.value || "").toUpperCase();
              if (pulseColorValue) pulseColorValue.textContent = value;
              if (pulseColorSwatch) pulseColorSwatch.style.background = value;
            }};
            if (pulseColorInput) pulseColorInput.addEventListener("input", syncPulseColorValue);
            nmToggle.addEventListener("change", syncNight);
            nightFields.forEach(el => el.addEventListener("input", syncNight));
            syncNight();

            const animationTag = document.getElementById("animationTag");
            const syncAnimationTag = () => {{
              if (animationTag) animationTag.textContent = "Mode: " + (anName.options[anName.selectedIndex]?.textContent || anName.value);
            }};
            anName.addEventListener("change", syncAnimationTag);
            syncAnimationTag();

            const syncPulseVisibility = () => {{
              const isPulse = anName.value === "pulse";
              if (pulseColorField) pulseColorField.classList.toggle("hidden", !isPulse);
              if (pulseColorNote) pulseColorNote.classList.toggle("hidden", !isPulse);
            }};
            anName.addEventListener("change", syncPulseVisibility);
            syncPulseVisibility();
            syncPulseColorValue();
          }})();
        </script>
      </body>
    </html>
    """


@app.post("/board/{board_id}")
async def board_settings_save(
    board_id: str,
    mode: str = Form("live"),
    brightness: int = Form(16),
    nm_enabled: T.Optional[str] = Form(None),
    nm_start: str = Form("23:00"),
    nm_end: str = Form("06:00"),
    nm_action: str = Form("off"),
    nm_dim: int = Form(20),
    an_name: str = Form("pulse"),
    an_speed: int = Form(120),
    an_color: str = Form("#FF5000"),
):
    board_id = _normalize_board_id(board_id)
    cfg = _get_or_create_board(board_id)

    mode = mode if mode in ("live", "animation") else "live"
    brightness = max(0, min(150, brightness))
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

def _as_no_store(resp: JSONResponse) -> JSONResponse:
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/board_settings")
def board_settings_api(board_id: str = Query(...)):
    board_id = _normalize_board_id(board_id)
    if not board_id:
        return _as_no_store(JSONResponse({"error": "missing board_id"}, status_code=400))
    try:
        cfg = _get_or_create_board(board_id)
    except ValueError:
        return _as_no_store(JSONResponse({"error": "invalid board_id"}, status_code=400))
    payload = dict(cfg)
    payload["settings_version"] = BOARD_SETTINGS_VERSION
    return _as_no_store(JSONResponse(payload))


def _station_subset(only: T.Optional[str]) -> dict[str, dict]:
    subset = STATION_INDEX
    if only:
        wanted = [x.strip() for x in only.split(",") if x.strip()]
        subset = {n: STATION_INDEX[n] for n in wanted if n in STATION_INDEX}
    return subset


def _calculate_states_for_subset(subset: dict[str, dict]) -> dict[str, int]:
    if not subset:
        return {}

    feed, _ = fetch_vp_cached()
    trains, _by_route, _ = collect_trains(feed, ROUTE_ID_TO_SHORT)

    if MB_VERBOSE:
        vlog(f"processing trains={len(trains)} stations={len(subset)}")

    if not trains:
        return {k: 0 for k in subset.keys()}

    VP_INCOMING = rt.VehiclePosition.INCOMING_AT
    VP_STOPPED = rt.VehiclePosition.STOPPED_AT

    out: dict[str, int] = {}
    matches = 0
    dist_fb = 0
    name_matches = 0  # NEW: track name-based matches

    for name, meta in subset.items():
        pid = meta["stop_id"]
        slat, slon = meta["lat"], meta["lon"]
        station_route = (meta.get("route_short") or "").upper()

        best = 0
        best_d = None

        for (tlat, tlon, train_route, child_sid, st) in trains:
            if train_route is not None and train_route != station_route:
                continue

            if not MB_FORCE_DISTANCE and child_sid:
                # Try 1: Direct parent lookup (numeric GTFS stop_ids)
                parent = CHILD_TO_PARENT.get(child_sid) or (child_sid if is_num_parent(child_sid) else None)
                
                # Try 2: Extract station name from location-based stop_ids (TfNSW format)
                if not parent or parent != pid:
                    extracted_name = extract_station_name_from_stop_id(child_sid)
                    if extracted_name:
                        parent = STATION_NAME_TO_PARENT.get(extracted_name)
                        if parent == pid:
                            name_matches += 1
                
                if parent == pid:
                    if st == VP_STOPPED:
                        best = 3
                        matches += 1
                        break
                    elif st == VP_INCOMING:
                        best = max(best, 2)
                        matches += 1

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
        vlog(f"result active={act}/{len(subset)} matches={matches} (name_based={name_matches}) distance_fb={dist_fb}")

    return out


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
        board_id = _normalize_board_id(board_id)
        if not board_id or board_id not in BOARD_SETTINGS:
            return JSONResponse({"error": "unknown board_id"}, status_code=403)
        # You *could* read mode here and e.g. short-circuit if mode=="animation".

    subset = _station_subset(only)
    if only and not subset:
        return JSONResponse({})

    return JSONResponse(_calculate_states_for_subset(subset))


@app.get("/board_payload")
def board_payload(
    board_id: str = Query(..., description="Board ID (5-char code)"),
    only: T.Optional[str] = Query(None, description="Comma-separated station names"),
):
    board_id = _normalize_board_id(board_id)
    if not board_id:
        return JSONResponse({"error": "missing board_id"}, status_code=400)
    try:
        cfg = _get_or_create_board(board_id)
    except ValueError:
        return JSONResponse({"error": "invalid board_id"}, status_code=400)

    subset = _station_subset(only)
    states_payload = _calculate_states_for_subset(subset)

    base_brightness = int(cfg.get("brightness", 16))
    nm_cfg = cfg.get("night_mode", {}) or {}
    effective_brightness, nm_active = _night_mode_effect(base_brightness, nm_cfg)

    return JSONResponse({
        "board_id": board_id,
        "mode": cfg.get("mode", "live"),
        "brightness": base_brightness,
        "brightness_effective": effective_brightness,
        "night_mode": nm_cfg,
        "night_mode_active": nm_active,
        "animation": cfg.get("animation", {}),
        "states": states_payload,
        "settings_version": BOARD_SETTINGS_VERSION,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    })

@app.get("/debug_names")
def debug_names():
    """Debug endpoint to see name extraction from realtime stop_ids"""
    feed, age = fetch_vp_cached()
    trains, _, _ = collect_trains(feed, ROUTE_ID_TO_SHORT)
    
    extractions = []
    for lat, lon, route, stop_id, status in trains[:50]:
        if stop_id:
            extracted = extract_station_name_from_stop_id(stop_id)
            parent = STATION_NAME_TO_PARENT.get(extracted) if extracted else None
            extractions.append({
                "stop_id": stop_id,
                "extracted_name": extracted,
                "mapped_parent": parent,
                "route": route
            })
    
    return {
        "name_to_parent_mapping_size": len(STATION_NAME_TO_PARENT),
        "sample_mappings": list(STATION_NAME_TO_PARENT.items())[:20],
        "extraction_samples": extractions
    }

# ---------------- run (dev) ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=os.getenv("RELOAD", "0") == "1")
