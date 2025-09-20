# app.py
import os
import json
import time
import re
import logging
from typing import Dict, List, Optional

import requests
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from google.transit import gtfs_realtime_pb2  # from gtfs-realtime-bindings
from threading import Thread

# Optional .env (won't crash if missing)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# ---------- Globals ----------
STATES: Dict[str, int] = {}   # global snapshot

# ---------- Config via ENV ----------
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

MB_VERBOSE = os.getenv("MB_VERBOSE", "0") in ("1", "true", "True")

ROUTE_SHORT_ALLOW = set(
    [s.strip() for s in os.getenv("ROUTE_SHORT_ALLOW", "T1,T2,T3,T4,T5,T6,T7,T8,T9,M1").split(",") if s.strip()]
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

# ---------- Logging ----------
logging.basicConfig(level=logging.DEBUG if MB_VERBOSE else logging.INFO)
log = logging.getLogger("sydtrains")

# ---------- App ----------
app = FastAPI(title="Sydney Trains / Metro States API", version="1.0.0")

# Simple in-memory cache for feed snapshots
_cache_until: float = 0.0
_cached_entities: List[dict] = []  # normalized train dicts

# ---------- Helpers ----------
ROUTE_SUFFIX = {
    "M1": "M",
    "T1": "1",
    "T2": "2",
    "T3": "3",
    "T4": "4",
    "T5": "5",
    "T6": "6",
    "T7": "7",
    "T8": "8",
    "T9": "9",
}

station_part_re = re.compile(
    r"""
    ^(?P<prefix>[^.]*)\.
    (?P<core>[A-Za-z /&\-\u2013'\.]+?)(?:\s+\d+)?      # "Town Hall", "Martin Place", etc.
    (?:\s+Loc.*)?$                                     # trailing "Loc" etc.
    """,
    re.VERBOSE,
)

def extract_station_core(stop_or_parent: str) -> Optional[str]:
    if not stop_or_parent:
        return None
    if "." in stop_or_parent:
        m = station_part_re.match(stop_or_parent.strip())
        if m:
            core = m.group("core").strip().rstrip(".").strip()
            return core
    s = re.sub(r"\s+\d+\s+Loc.*$", "", stop_or_parent.strip())
    return s if s else None

def route_to_short(route_id: Optional[str], feed_hint: Optional[str]) -> Optional[str]:
    if not route_id:
        if feed_hint and "metro" in feed_hint.lower():
            return "M1"
        return None
    if route_id in ROUTE_SHORT_OVERRIDES:
        return ROUTE_SHORT_OVERRIDES[route_id]
    rid = route_id.upper()
    if rid.startswith("S_T") and len(rid) >= 4 and rid[3].isdigit():
        return f"T{rid[3]}"
    if "METRO" in rid or "S_M1" in rid:
        return "M1"
    return None

def fetch_all_entities() -> List[dict]:
    global _cache_until, _cached_entities
    now = time.time()
    if now < _cache_until and _cached_entities:
        return _cached_entities

    headers = {"Authorization": f"apikey {TFNSW_API_TOKEN}"}
    all_entities: List[dict] = []

    for feed_url in FEEDS:
        try:
            resp = requests.get(feed_url, headers=headers, timeout=10)
            resp.raise_for_status()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(resp.content)
        except Exception as e:
            log.warning("Failed to fetch/parse feed %s: %s", feed_url, e)
            continue

        for ent in feed.entity:
            if not ent.HasField("vehicle"):
                continue
            veh = ent.vehicle

            # route short name
            route_short = route_to_short(veh.trip.route_id if veh.trip.route_id else None, feed_url)
            if not route_short or route_short not in ROUTE_SHORT_ALLOW:
                continue

            lat = veh.position.latitude if veh.position and veh.position.latitude else None
            lon = veh.position.longitude if veh.position and veh.position.longitude else None
            stop_id = veh.stop_id if veh.stop_id else None
            raw_stop = stop_id

            all_entities.append(
                {
                    "route_short": route_short,
                    "lat": float(lat) if lat is not None else None,
                    "lon": float(lon) if lon is not None else None,
                    "stop_id": stop_id,
                    "parent_station": None,
                    "raw_stop": raw_stop,
                    "feed": feed_url,
                }
            )

    _cached_entities = all_entities
    _cache_until = time.time() + FEED_CACHE_SEC
    return _cached_entities

def build_states() -> Dict[str, int]:
    entities = fetch_all_entities()
    counts: Dict[str, int] = {}
    for e in entities:
        suffix = ROUTE_SUFFIX.get(e["route_short"])
        if not suffix:
            continue
        station_raw = e.get("raw_stop") or e.get("stop_id") or e.get("parent_station")
        station_core = extract_station_core(station_raw or "")
        if not station_core:
            if MB_VERBOSE:
                log.debug("Skip entity without parsable station: %r", e)
            continue
        key = f"{station_core}{suffix}"
        counts[key] = counts.get(key, 0) + 1
    return counts

def _refresh_loop():
    global STATES
    interval = max(5, FEED_CACHE_SEC)   # poll at least every 5s
    while True:
        try:
            STATES = build_states()
            if MB_VERBOSE:
                log.info("Refreshed STATES: %d keys", len(STATES))
        except Exception:
            log.exception("Error refreshing STATES")
        time.sleep(interval)

# ---------- Routes ----------
@app.get("/")
def root():
    return {"ok": True, "keys": len(STATES)}

@app.on_event("startup")
def _start_refresher():
    # Prime once so /states isn’t empty on the first call
    try:
        global STATES
        STATES = build_states()
    except Exception:
        log.exception("Initial build_states failed")
    # Kick off the refresher thread
    t = Thread(target=_refresh_loop, daemon=True)
    t.start()

@app.get("/trains")
def get_trains():
    ents = fetch_all_entities()
    sample = ents[:50]
    return JSONResponse({"total_trains": len(ents), "sample_trains": sample})

@app.get("/states")
def get_states(only: Optional[str] = Query(None, description="Comma-separated list of station keys")):
    """
    Returns a BARE map (no wrapper), so the ESP can index keys directly.
    """
    global STATES
    if only:
        wanted = [x.strip() for x in only.split(",") if x.strip()]
        return {k: int(STATES.get(k, 0)) for k in wanted}
    # full snapshot (bare)
    return {k: int(v) for k, v in STATES.items()}

# ---------- Local run ----------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=os.getenv("RELOAD", "0") == "1")
