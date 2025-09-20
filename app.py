# app.py
import os, csv, json, time, re, logging
from typing import Dict, List, Optional
from threading import Thread
import re

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
GTFS_DIR = os.getenv("GTFS_DIR", ".")  # where stops.txt lives (default: cwd)

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
app = FastAPI(title="Sydney Trains / Metro States API", version="1.1.0")

# live snapshot served to the ESP
STATES: Dict[str, int] = {}

# tiny cache of the raw entities
_cache_until: float = 0.0
_cached_entities: List[dict] = []

# GTFS stop index
STOP_NAME_BY_ID: Dict[str, str] = {}      # stop_id -> best (parent) station name
STOP_PARENT: Dict[str, str] = {}          # stop_id -> parent_station (if any)

# -------------------- Helpers --------------------
ROUTE_SUFFIX = {"M1": "M", "T1": "1", "T2": "2", "T3": "3", "T4": "4", "T5": "5", "T6": "6", "T7": "7", "T8": "8", "T9": "9"}

_name_sanitize = re.compile(r"\s+\d+\s+Loc.*$")

_ROUTE_RE = re.compile(r"\b([MT])\s*([1-9])\b", re.IGNORECASE)

def route_to_short(route_id: Optional[str], feed_hint: Optional[str]) -> Optional[str]:
    """
    Try hard to get a short name:
    1) Explicit overrides (ROUTE_SHORT_OVERRIDES)
    2) Regex for things like "S_T2" or "T9" or "m1"
    3) Heuristic fallback from the feed URL (metro -> M1)
    """
    # 3) feed hint fallback (metro -> M1)
    feed_fallback = "M1" if (feed_hint and "metro" in feed_hint.lower()) else None

    # 1) explicit overrides first
    if route_id and route_id in ROUTE_SHORT_OVERRIDES:
        return ROUTE_SHORT_OVERRIDES[route_id]

    # 2) regex parse
    if route_id:
        rid = route_id.strip().upper()
        # common “S_Tx” shape
        if rid.startswith("S_T") and len(rid) >= 4 and rid[3].isdigit():
            return f"T{rid[3]}"
        if rid.startswith("S_M1") or "METRO" in rid:
            return "M1"
        m = _ROUTE_RE.search(rid)
        if m:
            letter, num = m.group(1).upper(), m.group(2)
            if letter == "M":
                return f"M{num}"
            return f"T{num}"

    # 3) give up → feed fallback (helps when route_id is empty/weird)
    return feed_fallback

def sanitize_name(s: str) -> str:
    # remove trailing “ 3 Loc”, trim BOM/whitespace
    s = s.strip().strip("\ufeff")
    return _name_sanitize.sub("", s).strip()

def _load_stops():
    """
    Build STOP_NAME_BY_ID by reading GTFS stops.txt (in GTFS_DIR).
    We resolve a child platform stop_id up to its parent station name when available.
    """
    global STOP_NAME_BY_ID, STOP_PARENT
    path = os.path.join(GTFS_DIR, "stops.txt")
    if not os.path.exists(path):
        log.warning("stops.txt not found at %s — station names may be numeric ids", path)
        STOP_NAME_BY_ID = {}
        STOP_PARENT = {}
        return

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rdr = csv.DictReader(f)
        rows = [r for r in rdr]

    # First pass: capture name & parent for each id
    name_raw: Dict[str, str] = {}
    parent_of: Dict[str, str] = {}
    for r in rows:
        sid = (r.get("stop_id") or "").strip()
        nm = sanitize_name(r.get("stop_name") or "")
        parent = (r.get("parent_station") or "").strip()
        if sid:
            name_raw[sid] = nm or sid
            if parent:
                parent_of[sid] = parent

    # Some parents are also rows; make sure we know their names too
    # (If not, fall back to the stop_id string.)
    def root_name(sid: str, depth: int = 0) -> str:
        # walk up at most 5 hops to prevent loops
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
    log.info("Loaded %d GTFS stops (resolving parents where present)", len(STOP_NAME_BY_ID))

def station_name_from_stop_id(stop_id: Optional[str]) -> Optional[str]:
    if not stop_id:
        return None
    # Exact match first
    nm = STOP_NAME_BY_ID.get(stop_id)
    if nm:
        return nm
    # Some feeds sometimes use parent ids directly; try that too
    parent = STOP_PARENT.get(stop_id)
    if parent and parent in STOP_NAME_BY_ID:
        return STOP_NAME_BY_ID[parent]
    # As a last resort, return the raw string (numeric)
    return stop_id

def fetch_all_entities() -> List[dict]:
    """
    Fetch both feeds, decode GTFS-RT, and keep entities whose route we can map.
    """
    global _cache_until, _cached_entities

    now = time.time()
    if _cached_entities and now < _cache_until:
        return _cached_entities

    headers = {
        "Authorization": f"apikey {TFNSW_API_TOKEN}",
        "Accept": "application/x-protobuf",   # <- important for some gateways
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

            short = route_to_short(
                veh.trip.route_id if (veh.trip and veh.trip.route_id) else None,
                feed_url,
            )
            # allow-list gate
            if not short or short not in ROUTE_SHORT_ALLOW:
                continue

            stop_id = veh.stop_id if veh.stop_id else None
            out.append(
                {
                    "route_short": short,
                    "stop_id": stop_id,
                    "feed": feed_url,
                    "lat": float(veh.position.latitude) if (veh.position and veh.position.latitude) else None,
                    "lon": float(veh.position.longitude) if (veh.position and veh.position.longitude) else None,
                }
            )
            per_feed += 1

        if MB_VERBOSE:
            log.info("Parsed %d vehicles from %s", per_feed, feed_url)

    _cached_entities = out
    _cache_until = time.time() + FEED_CACHE_SEC
    return _cached_entities

def build_states_snapshot() -> Dict[str, int]:
    entities = fetch_all_entities()
    counts: Dict[str, int] = {}

    for e in entities:
        short = e["route_short"]
        suf = ROUTE_SUFFIX.get(short)
        if not suf:
            continue

        # Resolve stop_id -> canonical station name from GTFS stops.txt
        station_name = station_name_from_stop_id(e.get("stop_id"))
        if not station_name:
            continue

        key = f"{station_name}{suf}"
        counts[key] = counts.get(key, 0) + 1

    return counts

# -------------------- background refresher --------------------
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
        "feeds": [
            {
                "feed": url,
                "ok": True,
                "status": 200,
            }
            for url in FEEDS
        ],
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
def get_states(only: Optional[str] = Query(None, description="Comma-separated list of station keys")):
    global STATES
    if only:
        wanted = [x.strip() for x in only.split(",") if x.strip()]
        return {"states": {k: int(STATES.get(k, 0)) for k in wanted}}
    return {"states": {k: int(v) for k, v in STATES.items()}}

# -------------------- local run --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=os.getenv("RELOAD", "0") == "1")
