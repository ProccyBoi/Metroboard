# app.py
import os
import json
import time
import re
import logging
from typing import Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from google.transit import gtfs_realtime_pb2  # from gtfs-realtime-bindings
from dotenv import load_dotenv

# Load .env in local/dev (Koyeb injects env vars automatically)
load_dotenv()

# ---------- Config via ENV ----------
TFNSW_API_TOKEN = os.getenv("TFNSW_API_TOKEN", "").strip()
if not TFNSW_API_TOKEN:
    raise RuntimeError("TFNSW_API_TOKEN is required")

# Feeds: default to Sydney Trains + Metro; override with FEEDS (comma-separated) if you like
FEEDS: List[str] = [
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
]
FEEDS_ENV = os.getenv("FEEDS", "").strip()
if FEEDS_ENV:
    FEEDS = [u.strip() for u in FEEDS_ENV.split(",") if u.strip()]

FEED_CACHE_SEC = int(os.getenv("FEED_CACHE_SEC", os.getenv("FEED_TTL_SEC", "3")))

# Distance knobs (wired for future geo logic; not required for this per-route splitting)
MB_D_SOLID = int(os.getenv("MB_D_SOLID", "300"))
MB_D_NEAR = int(os.getenv("MB_D_NEAR", "1000"))
MB_D_FAR = int(os.getenv("MB_D_FAR", "2000"))
MB_FORCE_DISTANCE = os.getenv("MB_FORCE_DISTANCE", "0") in ("1", "true", "True")
MB_PERMISSIVE_PAIRS = os.getenv("MB_PERMISSIVE_PAIRS", "1") in ("1", "true", "True")
MB_VERBOSE = os.getenv("MB_VERBOSE", "0") in ("1", "true", "True")

# Route allow-list (short names)
ROUTE_SHORT_ALLOW = set(
    [s.strip() for s in os.getenv("ROUTE_SHORT_ALLOW", "T1,T2,T3,T4,T5,T6,T7,T8,T9,M1").split(",") if s.strip()]
)

# Route overrides (route_id -> short name)
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
# Ensure defaults are present but allow env to override them
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
    # short name -> suffix used in your states dict keys
    # e.g., "Waterloo" + "M" => "WaterlooM", "Town Hall" + "2" => "Town Hall2"
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
    (?P<core>[A-Za-z /&\-\u2013'\.]+?)(?:\s+\d+)?      # "Town Hall", "Martin Place", "Bondi Junction", allow hyphens & slashes
    (?:\s+Loc.*)?$                                     # trailing "Loc" etc.
    """,
    re.VERBOSE,
)

def extract_station_core(stop_or_parent: str) -> Optional[str]:
    """
    Extract a human station name from strings like:
      "Sydenham.Martin Place 1 Loc" -> "Martin Place"
      "Sydney.Town Hall 5 Loc"      -> "Town Hall"
      "Sydenham.Bondi Junction 2 Loc" -> "Bondi Junction"
    If no dot present, try to lightly sanitize.
    """
    if not stop_or_parent:
        return None
    if "." in stop_or_parent:
        m = station_part_re.match(stop_or_parent.strip())
        if m:
            core = m.group("core").strip()
            # Clean accidental trailing dots
            core = core.rstrip(".").strip()
            return core
    # Fallback: remove trailing tokens like " 3 Loc"
    s = re.sub(r"\s+\d+\s+Loc.*$", "", stop_or_parent.strip())
    return s if s else None


def route_to_short(route_id: Optional[str], feed_hint: Optional[str]) -> Optional[str]:
    """
    Convert a GTFS-RT route_id to short name (T1..T9, M1).
    Uses overrides first, then heuristics (based on feed URL).
    """
    if not route_id:
        # Heuristic: if this came from the "metro" feed, consider it M1
        if feed_hint and "metro" in feed_hint.lower():
            return "M1"
        return None

    if route_id in ROUTE_SHORT_OVERRIDES:
        return ROUTE_SHORT_OVERRIDES[route_id]

    # Fallback heuristics: common patterns
    rid = route_id.upper()
    if rid.startswith("S_T") and len(rid) >= 4 and rid[3].isdigit():
        return f"T{rid[3]}"
    if "METRO" in rid or "S_M1" in rid:
        return "M1"

    return None


def fetch_all_entities() -> List[dict]:
    """
    Fetch and parse entities from all feeds, returning a normalized list:
    {
      "route_short": "T2"|"M1"|...,
      "lat": float,
      "lon": float,
      "stop_id": str|None,
      "parent_station": str|None,
      "raw_stop": str|None   # best available descriptive string for station parsing
    }
    """
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
            route_short = None
            if veh.trip.route_id:
                route_short = route_to_short(veh.trip.route_id, feed_url)
            else:
                route_short = route_to_short(None, feed_url)

            # filter to allow-list
            if not route_short or route_short not in ROUTE_SHORT_ALLOW:
                continue

            lat = veh.position.latitude if veh.position and veh.position.latitude else None
            lon = veh.position.longitude if veh.position and veh.position.longitude else None
            stop_id = veh.stop_id if veh.stop_id else None

            # In TfNSW strings you often see more descriptive parent-ish text in stop_id itself.
            # If there’s an extra descriptive field in your pipeline, add it here.
            raw_stop = stop_id

            all_entities.append(
                {
                    "route_short": route_short,
                    "lat": float(lat) if lat is not None else None,
                    "lon": float(lon) if lon is not None else None,
                    "stop_id": stop_id,
                    "parent_station": None,  # kept for compatibility
                    "raw_stop": raw_stop,
                    "feed": feed_url,
                }
            )

    _cached_entities = all_entities
    _cache_until = time.time() + FEED_CACHE_SEC
    return _cached_entities


def build_states() -> Dict[str, int]:
    """
    Produce a states dict like:
      {"Town HallM": 2, "Town Hall2": 1, "WaterlooM": 1, "Waterloo8": 0, ...}
    Note: We only output keys for stations we actually see. This keeps things dynamic and simple.
    """
    entities = fetch_all_entities()
    counts: Dict[str, int] = {}

    for e in entities:
        route_short = e["route_short"]
        suffix = ROUTE_SUFFIX.get(route_short)
        if not suffix:
            continue

        station_raw = e.get("raw_stop") or e.get("stop_id") or e.get("parent_station")
        station_core = extract_station_core(station_raw or "")
        if not station_core:
            # If we have no station name, skip counting — avoids bad keys
            if MB_VERBOSE:
                log.debug("Skip entity without parsable station: %r", e)
            continue

        key = f"{station_core}{suffix}"
        counts[key] = counts.get(key, 0) + 1

    return counts


# ---------- Routes ----------
@app.get("/trains")
def get_trains():
    """
    Returns a snapshot of normalized vehicle entities (debug-friendly).
    """
    ents = fetch_all_entities()
    sample = ents[:50]
    return JSONResponse(
        {
            "feed_age_seconds": 0.0,  # we don't track precise age; cache TTL keeps this fresh
            "total_trains": len(ents),
            "sample_trains": sample,
        }
    )


@app.get("/states")
def get_states(only: Optional[str] = Query(default=None, description="Comma-separated keys to return")):
    """
    Returns the states map with route-specific suffixes (M,1..9).
    If ?only= is provided, returns exactly those keys; missing ones are 0.
    """
    s = build_states()

    if not only:
        return JSONResponse(s)

    # Parse the requested keys and build a filtered dict, defaulting to 0
    requested = [k.strip() for k in only.split(",") if k.strip()]
    filtered = {k: int(s.get(k, 0)) for k in requested}
    return JSONResponse(filtered)


# ---------- Local run (Koyeb will launch via Procfile/Entrypoint) ----------
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=os.getenv("RELOAD", "0") == "1")
