# app.py
import os, csv, json, time, logging
from typing import Dict, List, Optional
from threading import Thread

import requests
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from google.transit import gtfs_realtime_pb2  # pip install gtfs-realtime-bindings
from dotenv import load_dotenv

# -------------------- ENV --------------------
load_dotenv()
TFNSW_API_TOKEN = os.getenv("TFNSW_API_TOKEN", "").strip()
if not TFNSW_API_TOKEN:
    raise RuntimeError("TFNSW_API_TOKEN is required")

# Feeds
FEEDS = {
    "metro_vehiclepos": "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
    "trains_tripupdates": "https://api.transport.nsw.gov.au/v2/gtfs/tripupdates/sydneytrains",
}
FEED_CACHE_SEC = int(os.getenv("FEED_CACHE_SEC", "5"))
MB_VERBOSE = os.getenv("MB_VERBOSE", "0") in ("1", "true", "True")
GTFS_DIR = os.getenv("GTFS_DIR", ".")

ROUTE_SHORT_ALLOW = set(
    [s.strip() for s in os.getenv("ROUTE_SHORT_ALLOW", "T1,T2,T3,T4,T5,T6,T7,T8,T9,M1").split(",") if s.strip()]
)

ROUTE_SUFFIX = {"M1": "M", "T1": "1", "T2": "2", "T3": "3", "T4": "4", "T5": "5", "T6": "6", "T7": "7", "T8": "8", "T9": "9"}

logging.basicConfig(level=logging.DEBUG if MB_VERBOSE else logging.INFO)
log = logging.getLogger("sydtrains")

# -------------------- State --------------------
app = FastAPI(title="Sydney Trains / Metro States API", version="2.0.0")

STATES: Dict[str, int] = {}
STOP_NAME_BY_ID: Dict[str, str] = {}
STOP_PARENT: Dict[str, str] = {}
_cache_until: float = 0.0
_cached_entities: List[dict] = []


# -------------------- Helpers --------------------
def _load_stops():
    """Load stops.txt into STOP_NAME_BY_ID (resolving parents)."""
    global STOP_NAME_BY_ID, STOP_PARENT
    path = os.path.join(GTFS_DIR, "stops.txt")
    if not os.path.exists(path):
        log.warning("stops.txt not found — station names may be numeric IDs")
        return

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rdr = csv.DictReader(f)
        rows = [r for r in rdr]

    name_raw: Dict[str, str] = {}
    parent_of: Dict[str, str] = {}
    for r in rows:
        sid = (r.get("stop_id") or "").strip()
        nm = (r.get("stop_name") or "").strip()
        parent = (r.get("parent_station") or "").strip()
        if sid:
            name_raw[sid] = nm or sid
            if parent:
                parent_of[sid] = parent

    def root_name(sid: str) -> str:
        seen = set()
        cur = sid
        for _ in range(5):
            if cur in seen:
                break
            seen.add(cur)
            parent = parent_of.get(cur, "")
            if not parent:
                break
            cur = parent
        return name_raw.get(cur, cur)

    STOP_PARENT = parent_of
    STOP_NAME_BY_ID = {sid: root_name(sid) for sid in name_raw.keys()}
    log.info("Loaded %d GTFS stops", len(STOP_NAME_BY_ID))


def station_name_from_stop_id(stop_id: Optional[str]) -> Optional[str]:
    if not stop_id:
        return None
    if stop_id in STOP_NAME_BY_ID:
        return STOP_NAME_BY_ID[stop_id]
    parent = STOP_PARENT.get(stop_id)
    if parent and parent in STOP_NAME_BY_ID:
        return STOP_NAME_BY_ID[parent]
    return stop_id


def route_to_short(route_id: Optional[str]) -> Optional[str]:
    """Map GTFS route_id → short code (T1…T9, M1)."""
    if not route_id:
        return None
    rid = route_id.upper()
    if rid.startswith("S_T") and len(rid) >= 4 and rid[3].isdigit():
        return f"T{rid[3]}"
    if "METRO" in rid or rid.startswith("S_M1"):
        return "M1"
    return None


# -------------------- Feeds --------------------
def fetch_all_entities() -> List[dict]:
    """Fetch vehiclepos (Metro) + tripupdates (Sydney Trains)."""
    global _cache_until, _cached_entities
    now = time.time()
    if _cached_entities and now < _cache_until:
        return _cached_entities

    headers = {
        "Authorization": f"apikey {TFNSW_API_TOKEN}",
        "Accept": "application/x-protobuf",
    }
    out: List[dict] = []

    # Metro vehicle positions
    try:
        resp = requests.get(FEEDS["metro_vehiclepos"], headers=headers, timeout=15)
        resp.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)
        for ent in feed.entity:
            if not ent.HasField("vehicle"):
                continue
            veh = ent.vehicle
            short = "M1"
            if short not in ROUTE_SHORT_ALLOW:
                continue
            out.append(
                {
                    "route_short": short,
                    "stop_id": veh.stop_id if veh.stop_id else None,
                    "lat": float(veh.position.latitude) if veh.position and veh.position.latitude else None,
                    "lon": float(veh.position.longitude) if veh.position and veh.position.longitude else None,
                }
            )
    except Exception as e:
        log.warning("Metro vehiclepos fetch failed: %s", e)

    # Sydney Trains trip updates
    try:
        resp = requests.get(FEEDS["trains_tripupdates"], headers=headers, timeout=15)
        resp.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)
        for ent in feed.entity:
            if not ent.HasField("trip_update"):
                continue
            tu = ent.trip_update
            short = route_to_short(tu.trip.route_id)
            if not short or short not in ROUTE_SHORT_ALLOW:
                continue
            for stu in tu.stop_time_update:
                sid = stu.stop_id
                out.append(
                    {
                        "route_short": short,
                        "stop_id": sid,
                        "lat": None,
                        "lon": None,
                    }
                )
    except Exception as e:
        log.warning("Sydney Trains tripupdates fetch failed: %s", e)

    _cached_entities = out
    _cache_until = time.time() + FEED_CACHE_SEC
    return _cached_entities


def build_states_snapshot() -> Dict[str, int]:
    ents = fetch_all_entities()
    counts: Dict[str, int] = {}
    for e in ents:
        short = e["route_short"]
        suf = ROUTE_SUFFIX.get(short)
        if not suf:
            continue
        station_name = station_name_from_stop_id(e.get("stop_id"))
        if not station_name:
            continue
        key = f"{station_name}{suf}"
        counts[key] = counts.get(key, 0) + 1
    return counts


# -------------------- Background refresher --------------------
def _refresh_loop():
    global STATES
    interval = max(5, FEED_CACHE_SEC)
    while True:
        try:
            STATES = build_states_snapshot()
            if MB_VERBOSE:
                log.info("STATES refreshed: %d keys", len(STATES))
        except Exception:
            log.exception("Error refreshing STATES")
        time.sleep(interval)


# -------------------- Endpoints --------------------
@app.on_event("startup")
def _on_start():
    _load_stops()
    t = Thread(target=_refresh_loop, daemon=True)
    t.start()


@app.get("/debug")
def debug():
    ents = fetch_all_entities()
    return {
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
def get_states(only: Optional[str] = Query(None)):
    global STATES
    if only:
        wanted = [x.strip() for x in only.split(",") if x.strip()]
        return {"states": {k: int(STATES.get(k, 0)) for k in wanted}}
    return {"states": {k: int(v) for k, v in STATES.items()}}


# -------------------- Local run --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=os.getenv("RELOAD", "0") == "1")
