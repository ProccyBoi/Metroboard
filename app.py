# app.py
import os, json, time, re, logging
from typing import Dict, List, Optional
from threading import Thread

import requests
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from google.transit import gtfs_realtime_pb2  # pip: gtfs-realtime-bindings
from dotenv import load_dotenv

load_dotenv()

# -------------------- ENV / Config --------------------
TFNSW_API_TOKEN = os.getenv("TFNSW_API_TOKEN", "").strip()
if not TFNSW_API_TOKEN:
    raise RuntimeError("TFNSW_API_TOKEN is required")

FEEDS: List[str] = [
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
]
if os.getenv("FEEDS"):
    FEEDS = [u.strip() for u in os.getenv("FEEDS", "").split(",") if u.strip()]

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

logging.basicConfig(level=logging.DEBUG if MB_VERBOSE else logging.INFO)
log = logging.getLogger("sydtrains")

# -------------------- App & global state --------------------
app = FastAPI(title="Sydney Trains / Metro States API", version="1.2.0")

STATES: Dict[str, int] = {}
_cache_until: float = 0.0
_cached_entities: List[dict] = []

# -------------------- Helpers --------------------
ROUTE_SUFFIX = {"M1": "M", "T1": "1", "T2": "2", "T3": "3", "T4": "4", "T5": "5", "T6": "6", "T7": "7", "T8": "8", "T9": "9"}

station_part_re = re.compile(
    r"""
    ^(?P<prefix>[^.]*)\.
    (?P<core>[A-Za-z /&\-\u2013'\.]+?)(?:\s+\d+)?   # "Town Hall", "Martin Place"
    (?:\s+Loc.*)?$
    """,
    re.VERBOSE,
)

def extract_station_core(stop_or_parent: str) -> Optional[str]:
    """Extract clean station names like 'Town Hall' from 'Sydney.Town Hall 3 Loc'."""
    if not stop_or_parent:
        return None
    if "." in stop_or_parent:
        m = station_part_re.match(stop_or_parent.strip())
        if m:
            return m.group("core").strip().rstrip(".")
    # fallback: strip trailing " 3 Loc"
    return re.sub(r"\s+\d+\s+Loc.*$", "", stop_or_parent.strip())

def route_to_short(route_id: Optional[str], feed_hint: Optional[str]) -> Optional[str]:
    feed_fallback = "M1" if (feed_hint and "metro" in feed_hint.lower()) else None
    if route_id and route_id in ROUTE_SHORT_OVERRIDES:
        return ROUTE_SHORT_OVERRIDES[route_id]
    if route_id:
        rid = route_id.strip().upper()
        if rid.startswith("S_T") and len(rid) >= 4 and rid[3].isdigit():
            return f"T{rid[3]}"
        if rid.startswith("S_M1") or "METRO" in rid:
            return "M1"
    return feed_fallback

def fetch_all_entities() -> List[dict]:
    global _cache_until, _cached_entities
    now = time.time()
    if _cached_entities and now < _cache_until:
        return _cached_entities

    headers = {"Authorization": f"apikey {TFNSW_API_TOKEN}", "Accept": "application/x-protobuf"}
    out: List[dict] = []

    for feed_url in FEEDS:
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
            short = route_to_short(veh.trip.route_id if veh.trip and veh.trip.route_id else None, feed_url)
            if not short or short not in ROUTE_SHORT_ALLOW:
                continue
            out.append(
                {
                    "route_short": short,
                    "stop_id": veh.stop_id if veh.stop_id else None,
                    "feed": feed_url,
                    "lat": float(veh.position.latitude) if veh.position and veh.position.latitude else None,
                    "lon": float(veh.position.longitude) if veh.position and veh.position.longitude else None,
                }
            )
    _cached_entities = out
    _cache_until = time.time() + FEED_CACHE_SEC
    return _cached_entities

def build_states_snapshot() -> Dict[str, int]:
    entities = fetch_all_entities()
    counts: Dict[str, int] = {}
    for e in entities:
        suf = ROUTE_SUFFIX.get(e["route_short"])
        if not suf:
            continue
        station_core = extract_station_core(e.get("stop_id") or "")
        if not station_core:
            continue
        key = f"{station_core}{suf}"
        counts[key] = counts.get(key, 0) + 1
    return counts

# -------------------- background refresher --------------------
def _refresh_loop():
    global STATES
    interval = max(5, FEED_CACHE_SEC)
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
    t = Thread(target=_refresh_loop, daemon=True)
    t.start()

@app.get("/debug")
def debug():
    ents = fetch_all_entities()
    return {
        "feeds": [{"feed": url, "ok": True, "status": 200} for url in FEEDS],
        "last_build": {"entities": len(ents), "keys": len(STATES), "when": int(time.time())},
        "states_keys": len(STATES),
        "routes_allowed": sorted(list(ROUTE_SHORT_ALLOW)),
        "feeds_config": FEEDS,
        "cache_sec": FEED_CACHE_SEC,
        "token_present": bool(TFNSW_API_TOKEN),
        "verbose": MB_VERBOSE,
    }

@app.get("/trains")
def get_trains():
    ents = fetch_all_entities()
    return JSONResponse({"total_trains": len(ents), "sample_trains": ents[:50]})

@app.get("/states")
def get_states(only: Optional[str] = Query(None)):
    global STATES
    if only:
        wanted = [x.strip() for x in only.split(",") if x.strip()]
        return {"states": {k: int(STATES.get(k, 0)) for k in wanted}}
    return {"states": {k: int(v) for k, v in STATES.items()}}

# -------------------- local run --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=os.getenv("RELOAD", "0") == "1")
