# app.py
import os
import csv
import json
import time
import math
import re
import logging
from typing import Dict, List, Optional, Tuple
from threading import Thread

import requests
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from google.transit import gtfs_realtime_pb2  # pip install gtfs-realtime-bindings
from dotenv import load_dotenv

# -------------------- env --------------------
load_dotenv()

TFNSW_API_TOKEN = os.getenv("TFNSW_API_TOKEN", "").strip()
if not TFNSW_API_TOKEN:
    raise RuntimeError("TFNSW_API_TOKEN is required")

FEEDS: List[str] = [
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
]
FEEDS_ENV = os.getenv("FEEDS", "").strip()
if FEEDS_ENV:
    FEEDS = [u.strip() for u in FEEDS_ENV.split(",") if u.strip()]

FEED_CACHE_SEC = int(os.getenv("FEED_CACHE_SEC", os.getenv("FEED_TTL_SEC", "3")))
MB_VERBOSE = os.getenv("MB_VERBOSE", "0").lower() in ("1", "true", "yes")
GTFS_DIR = os.getenv("GTFS_DIR", ".")  # directory containing stops.txt

ROUTE_SHORT_ALLOW = set(
    s.strip() for s in os.getenv("ROUTE_SHORT_ALLOW", "T1,T2,T3,T4,T5,T6,T7,T8,T9,M1").split(",") if s.strip()
)

_default_overrides = {
    "S_T1_MAIN": "T1",
    "S_T1_NORTH": "T1",
    "S_T2": "T2",
    "S_T3": "T3",
    "S_T4": "T4",
    "S_T5": "T5",
    "S_T6": "T6",
    "S_T7": "T7",
    "S_T8": "T8",
    "S_T9": "T9",
    "S_M1": "M1",
}
try:
    ROUTE_SHORT_OVERRIDES = json.loads(os.getenv("ROUTE_SHORT_OVERRIDES_JSON", "{}")) or {}
except json.JSONDecodeError:
    ROUTE_SHORT_OVERRIDES = {}
ROUTE_SHORT_OVERRIDES = {**_default_overrides, **ROUTE_SHORT_OVERRIDES}

logging.basicConfig(level=logging.DEBUG if MB_VERBOSE else logging.INFO)
log = logging.getLogger("sydtrains")

# -------------------- app & globals --------------------
app = FastAPI(title="Sydney Trains / Metro States API", version="1.3.0")

# live snapshot (what the ESP polls)
STATES: Dict[str, int] = {}

# small entity cache (to respect FEED_CACHE_SEC)
_cache_until: float = 0.0
_cached_entities: List[dict] = []

# GTFS lookups
STOP_NAME_BY_ID: Dict[str, str] = {}           # stop_id -> canonical station name (parent-resolved)
STOP_PARENT: Dict[str, str] = {}               # stop_id -> parent_station (if any)
STATION_POINTS: Dict[str, Tuple[float, float]] = {}  # station_name -> (lat, lon)

# -------------------- helpers --------------------
ROUTE_SUFFIX = {"M1": "M", "T1": "1", "T2": "2", "T3": "3", "T4": "4", "T5": "5", "T6": "6", "T7": "7", "T8": "8", "T9": "9"}

# permissive route-id sniffers (covers "S_T2", "T9", "T1 NORTH", etc.)
_RE_T = re.compile(r"\bT\s*([1-9])\b", re.IGNORECASE)
_RE_M = re.compile(r"\bM\s*([1-9])\b", re.IGNORECASE)

# Sydney Trains descriptive stop_id parser: "Sydney.Town Hall 5 Loc" -> "Town Hall"
_station_part_re = re.compile(
    r"""
    ^[^.]*\.
    (?P<core>[A-Za-z /&\-\u2013'\.]+?)      # station words
    (?:\s+\d+)?                             # platform numbers
    (?:\s+Loc.*)?\s*$                       # trailing "Loc" noise
    """,
    re.VERBOSE,
)

def _sanitize_name(s: str) -> str:
    s = s.strip().strip("\ufeff")
    # remove trailing " 3 Loc ..."
    s = re.sub(r"\s+\d+\s+Loc.*$", "", s)
    return s.strip().rstrip(".")

def extract_station_core(stop_or_parent: Optional[str]) -> Optional[str]:
    if not stop_or_parent:
        return None
    txt = stop_or_parent.strip()
    if "." in txt:
        m = _station_part_re.match(txt)
        if m:
            return _sanitize_name(m.group("core"))
    return _sanitize_name(txt) if txt else None

def route_to_short(route_id: Optional[str], feed_url: Optional[str]) -> Optional[str]:
    # explicit overrides first
    if route_id and route_id in ROUTE_SHORT_OVERRIDES:
        return ROUTE_SHORT_OVERRIDES[route_id]

    # permissive regex sniffing
    if route_id:
        rid = route_id.strip().upper().replace("_", " ").replace("-", " ")
        m = _RE_T.search(rid)
        if m:
            return f"T{m.group(1)}"
        m = _RE_M.search(rid)
        if m:
            return f"M{m.group(1)}"
        if "METRO" in rid:
            return "M1"

    # feed hint
    if feed_url and "metro" in feed_url.lower():
        return "M1"
    return None

# cheap-ish spherical distance (no heavy geodesy needed here)
def _dist2(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    # equirectangular approximation
    rlat = math.radians((lat1 + lat2) * 0.5)
    dx = (lon2 - lon1) * math.cos(rlat)
    dy = (lat2 - lat1)
    return dx * dx + dy * dy

def nearest_station_name(lat: Optional[float], lon: Optional[float]) -> Optional[str]:
    if lat is None or lon is None or not STATION_POINTS:
        return None
    best_name, best_d2 = None, float("inf")
    for name, (slat, slon) in STATION_POINTS.items():
        if slat is None or slon is None:
            continue
        d2 = _dist2(lat, lon, slat, slon)
        if d2 < best_d2:
            best_name, best_d2 = name, d2
    return best_name

def _load_stops():
    """Load stops.txt once; resolve child->parent station names and build a station centroid map."""
    global STOP_NAME_BY_ID, STOP_PARENT, STATION_POINTS

    path = os.path.join(GTFS_DIR, "stops.txt")
    if not os.path.exists(path):
        log.warning("stops.txt not found at %s — geo fallback limited; Sydney Trains will still parse descriptive stop_ids.", path)
        STOP_NAME_BY_ID = {}
        STOP_PARENT = {}
        STATION_POINTS = {}
        return

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rdr = csv.DictReader(f)
        rows = [r for r in rdr]

    name_raw: Dict[str, str] = {}
    parent_of: Dict[str, str] = {}
    coords: Dict[str, Tuple[Optional[float], Optional[float]]] = {}

    for r in rows:
        sid = (r.get("stop_id") or "").strip()
        nm = _sanitize_name(r.get("stop_name") or "") or sid
        parent = (r.get("parent_station") or "").strip()
        lat = r.get("stop_lat")
        lon = r.get("stop_lon")
        flat = float(lat) if lat not in (None, "",) else None
        flon = float(lon) if lon not in (None, "",) else None
        if sid:
            name_raw[sid] = nm
            coords[sid] = (flat, flon)
            if parent:
                parent_of[sid] = parent

    # resolve to parent/root id
    def root_id(sid: str) -> str:
        seen = set()
        cur = sid
        for _ in range(6):
            if cur in seen:
                break
            seen.add(cur)
            p = parent_of.get(cur, "")
            if not p:
                break
            cur = p
        return cur

    STOP_PARENT = parent_of
    STOP_NAME_BY_ID = {sid: name_raw.get(root_id(sid), name_raw.get(sid, sid)) for sid in name_raw.keys()}

    # build station points (choose the first available coord for a root name)
    STATION_POINTS = {}
    for sid, nm in STOP_NAME_BY_ID.items():
        lat, lon = coords.get(sid, (None, None))
        if nm not in STATION_POINTS and lat is not None and lon is not None:
            STATION_POINTS[nm] = (lat, lon)

    log.info("Loaded %d GTFS stops with coords", len(STOP_NAME_BY_ID))

def station_name_from_stop_id(stop_id: Optional[str]) -> Optional[str]:
    if not stop_id:
        return None
    # 1) direct id → parent-resolved station name
    nm = STOP_NAME_BY_ID.get(stop_id)
    if nm:
        return nm
    # 2) Sydney Trains descriptive strings like "Sydney.Town Hall 5 Loc"
    nm = extract_station_core(stop_id)
    if nm:
        return nm
    # 3) parent id direct (sometimes feeds use parent ids)
    parent = STOP_PARENT.get(stop_id)
    if parent:
        return STOP_NAME_BY_ID.get(parent, parent)
    return None

# -------------------- feed fetch --------------------
def fetch_all_entities() -> List[dict]:
    """Fetch both feeds; return normalized vehicle dicts with best-effort route + station derivation."""
    global _cache_until, _cached_entities

    now = time.time()
    if _cached_entities and now < _cache_until:
        return _cached_entities

    headers = {
        "Authorization": f"apikey {TFNSW_API_TOKEN}",
        "Accept": "application/x-protobuf",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    out: List[dict] = []

    for feed_url in FEEDS:
        per_feed = 0
        try:
            resp = requests.get(feed_url, headers=headers, timeout=15)
            resp.raise_for_status()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(resp.content)
        except Exception as e:
            log.warning("Feed fetch/parse failed for %s: %s", feed_url, e)
            continue

        for ent in feed.entity:
            if not ent.HasField("vehicle"):
                continue
            veh = ent.vehicle

            route_short = route_to_short(
                veh.trip.route_id if (veh.trip and veh.trip.route_id) else None,
                feed_url,
            )
            # If we *still* can't map a route for Sydney Trains, skip — we can't make a correct suffix.
            if not route_short or route_short not in ROUTE_SHORT_ALLOW:
                continue

            lat = float(veh.position.latitude) if (veh.position and veh.position.latitude) else None
            lon = float(veh.position.longitude) if (veh.position and veh.position.longitude) else None
            raw_stop_id = veh.stop_id if veh.stop_id else None

            out.append(
                {
                    "route_short": route_short,
                    "stop_id": raw_stop_id,
                    "lat": lat,
                    "lon": lon,
                    "feed": feed_url,
                }
            )
            per_feed += 1

        if MB_VERBOSE:
            log.info("Parsed %d vehicles from %s", per_feed, feed_url)

    _cached_entities = out
    _cache_until = time.time() + FEED_CACHE_SEC
    return _cached_entities

# -------------------- states build --------------------
def build_states_snapshot() -> Dict[str, int]:
    entities = fetch_all_entities()
    counts: Dict[str, int] = {}

    for e in entities:
        short = e["route_short"]
        suf = ROUTE_SUFFIX.get(short)
        if not suf:
            continue

        # station name priority:
        # 1) map stop_id via GTFS (works for Metro & some Sydney Trains)
        # 2) parse descriptive stop_id text (Sydney Trains: "Sydney.Town Hall 5 Loc")
        # 3) geo fallback (nearest station)
        station_name = station_name_from_stop_id(e.get("stop_id"))
        if not station_name:
            station_name = nearest_station_name(e.get("lat"), e.get("lon"))
        if not station_name:
            continue

        key = f"{station_name}{suf}"
        counts[key] = counts.get(key, 0) + 1

    return counts

# -------------------- refresher thread --------------------
def _refresh_loop():
    global STATES
    interval = max(5, FEED_CACHE_SEC)  # at least every 5s
    while True:
        try:
            STATES = build_states_snapshot()
            if MB_VERBOSE:
                log.info("STATES refreshed: %d keys", len(STATES))
        except Exception:
            log.exception("Error while refreshing STATES")
        time.sleep(interval)

# -------------------- endpoints --------------------
@app.on_event("startup")
def _on_start():
    _load_stops()
    t = Thread(target=_refresh_loop, daemon=True)
    t.start()

@app.get("/debug")
def debug():
    ents = fetch_all_entities()
    return {
        "feeds": [{"feed": u, "ok": True, "status": 200} for u in FEEDS],
        "last_build": {"entities": len(ents), "keys": len(STATES), "when": int(time.time())},
        "states_keys": len(STATES),
        "routes_allowed": sorted(list(ROUTE_SHORT_ALLOW)),
        "feeds_config": FEEDS,
        "cache_sec": FEED_CACHE_SEC,
        "stops_indexed": len(STOP_NAME_BY_ID),
        "token_present": bool(TFNSW_API_TOKEN),
        "verbose": MB_VERBOSE,
    }

@app.get("/trains")
def get_trains():
    ents = fetch_all_entities()
    return JSONResponse({"total_trains": len(ents), "sample_trains": ents[:50]})

@app.get("/states")
def get_states(only: Optional[str] = Query(None, description="Comma-separated keys")):
    if only:
        wanted = [x.strip() for x in (only or "").split(",") if x.strip()]
        return {"states": {k: int(STATES.get(k, 0)) for k in wanted}}
    return {"states": {k: int(v) for k, v in STATES.items()}}

# -------------------- local run --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=os.getenv("RELOAD", "0") == "1")
