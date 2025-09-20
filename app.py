#!/usr/bin/env python3
import os
import json
import time
from typing import Dict, Optional, Tuple, List
import requests
from google.transit import gtfs_realtime_pb2  # from gtfs-realtime-bindings
from dotenv import load_dotenv

# ---------- Load env ----------
load_dotenv()

TFNSW_API_TOKEN = os.getenv("TFNSW_API_TOKEN", "").strip()
if not TFNSW_API_TOKEN:
    raise SystemExit("Missing TFNSW_API_TOKEN")

# Distance thresholds etc. (present but not used here; keep for parity with your setup)
FEED_CACHE_SEC = int(os.getenv("FEED_CACHE_SEC", "3"))

# Station/route config
MB_PERMISSIVE_PAIRS = os.getenv("MB_PERMISSIVE_PAIRS", "1") == "1"
ROUTE_SHORT_ALLOW = set(s.strip() for s in os.getenv("ROUTE_SHORT_ALLOW", "T1,T2,T3,T4,T5,T6,T7,T8,T9,M1").split(",") if s.strip())

# Overrides – exactly the JSON you pasted in env
# {"S_T1_MAIN":"T1","S_T1_NORTH":"T1", ... "S_M1":"M1"}
try:
    ROUTE_SHORT_OVERRIDES: Dict[str, str] = json.loads(os.getenv("ROUTE_SHORT_OVERRIDES_JSON", "{}"))
except Exception as e:
    raise SystemExit(f"Bad ROUTE_SHORT_OVERRIDES_JSON: {e}")

# Feeds
VEHICLE_FEEDS = [
    os.getenv("FEED_SYDNEYTRAINS_VEH", "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains"),
    os.getenv("FEED_METRO_VEH",        "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro"),
]
TRIP_UPDATES_FEED = os.getenv("FEED_SYDNEYTRAINS_TRIPS", "https://api.transport.nsw.gov.au/v2/gtfs/realtime/sydneytrains")

HEADERS = {"Authorization": f"Bearer {TFNSW_API_TOKEN}"}

# ---------- Trip-id → route-id snapshot ----------
_trip_to_route: Dict[str, str] = {}

def refresh_trip_updates() -> None:
    """
    Refresh _trip_to_route from TFNSW Trip Updates (Sydney Trains).
    This is the key piece that lets vehicles with route_id=None inherit a route.
    """
    global _trip_to_route
    feed = gtfs_realtime_pb2.FeedMessage()
    r = requests.get(TRIP_UPDATES_FEED, headers=HEADERS, timeout=20)
    r.raise_for_status()
    feed.ParseFromString(r.content)

    snap: Dict[str, str] = {}
    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        trip_id = tu.trip.trip_id if tu.trip and tu.trip.trip_id else None
        route_id = tu.trip.route_id if tu.trip and tu.trip.route_id else None
        if trip_id and route_id:
            snap[trip_id] = route_id

    _trip_to_route = snap  # swap snapshot atomically

def route_id_from_trip(trip_id: Optional[str]) -> Optional[str]:
    if not trip_id:
        return None
    return _trip_to_route.get(trip_id)

# ---------- Vehicle fetch ----------
def fetch_vehicle_positions(url: str) -> List[gtfs_realtime_pb2.VehiclePosition]:
    feed = gtfs_realtime_pb2.FeedMessage()
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    feed.ParseFromString(r.content)
    out = []
    for ent in feed.entity:
        if ent.HasField("vehicle"):
            out.append(ent.vehicle)
    return out

# ---------- Mapping to route_short ----------
def default_short_from_route_id(route_id: str) -> str:
    # If the upstream uses "S_T8" etc, strip leading "S_".
    return route_id[2:] if route_id.startswith("S_") else route_id

def station_name_from_vehicle(v: gtfs_realtime_pb2.VehiclePosition) -> Optional[str]:
    """
    Your app already maps track/segments to station keys.
    Here we try best-effort from stop_id or stop_name if present.
    Replace this with your own station resolution to match your keys,
    e.g. "Waterloo", "Central", etc.
    """
    # Try stop_id first
    sid = v.stop_id.strip() if v.stop_id else None
    if sid:
        # Many TFNSW stop_ids are like "211011" – you probably already map these
        return sid
    # If your feed includes position + custom parent_station elsewhere, plug it here.
    return None

def guess_tline_from_station(station: str) -> Optional[str]:
    """
    Conservative fallback only if MB_PERMISSIVE_PAIRS is on and trip join failed.
    Extend/replace with your CSV-based allowed pairs if you have it available here.
    """
    if not MB_PERMISSIVE_PAIRS:
        return None

    s = station.lower()

    # Handful of useful heuristics; add more as needed.
    if any(s.startswith(x) for x in ["cronulla","caringbah","woolooware","gymea","miranda","kirrawee","como","jannali","sutherland"]):
        return "T4"
    if any(x in s for x in ["hornsby","wahroonga","warrawee","turramurra","gordon","lindfield","chatswood","wynyard","town hall","redfern","milsons point","north sydney"]):
        return "T1"
    if any(x in s for x in ["epping","eastwood","west ryde","rhodes","strathfield","central"]):
        return "T9"
    return None

def route_short_for_vehicle(v: gtfs_realtime_pb2.VehiclePosition) -> Optional[str]:
    """
    Decide route_short for a vehicle using:
      1) vehicle.route_id if present
      2) join from trip_id via Trip Updates
      3) (fallback) guess by station if allowed
    """
    # 1) From vehicle
    rid = v.trip.route_id if (v.trip and v.trip.route_id) else None
    # 2) Join from trip
    if not rid:
        rid = route_id_from_trip(v.trip.trip_id if v.trip else None)

    # 3) Map to short
    if rid:
        short = ROUTE_SHORT_OVERRIDES.get(rid) or default_short_from_route_id(rid)
        # Keep only allowed set if provided
        if short in ROUTE_SHORT_ALLOW:
            return short
        # if not allowed, drop it
        return None

    # 4) Fallback by station (Sydney Trains only)
    st = station_name_from_vehicle(v)
    if st:
        guess = guess_tline_from_station(st)
        if guess and guess in ROUTE_SHORT_ALLOW:
            return guess

    return None

# ---------- Counting ----------
def station_bucket_key(station_base: str, route_short: str) -> Optional[str]:
    """
    Produce keys like "Waterloo8" for T8 and "WaterlooM" for M1, so M stays independent.
    If your station keys are already of that form, you can plug your mapper here.
    """
    if route_short == "M1":
        return f"{station_base}M"
    if route_short.startswith("T") and len(route_short) == 2 and route_short[1].isdigit():
        return f"{station_base}{route_short[1]}"
    return None

def _station_base_from_resolved(station_resolved: str) -> str:
    """
    You likely already have exact display names ("Waterloo", "Central", etc.).
    If you’re resolving from numeric stop_ids, replace this with your own mapping,
    e.g. stop_id_to_name[stop_id].
    """
    return station_resolved

def count_trains() -> Tuple[Dict[str, int], Dict[str, int]]:
    """
    Returns:
      states: per-station bucket counts (Waterloo8, WaterlooM, etc.)
      by_route: total per route_short (e.g. {"M1": 13, "T8": 42, ...})
    """
    states: Dict[str, int] = {}
    by_route: Dict[str, int] = {}

    # Refresh trip updates FIRST (critical for Sydney Trains route join)
    refresh_trip_updates()

    # Then collect vehicles from both feeds
    all_vehicles: List[gtfs_realtime_pb2.VehiclePosition] = []
    for url in VEHICLE_FEEDS:
        try:
            all_vehicles.extend(fetch_vehicle_positions(url))
        except Exception as e:
            print(f"warn: vehicle feed failed {url}: {e}")

    for v in all_vehicles:
        rshort = route_short_for_vehicle(v)
        if not rshort:
            continue  # can't classify

        by_route[rshort] = by_route.get(rshort, 0) + 1

        # Resolve a station base name/key for your buckets
        st_resolved = station_name_from_vehicle(v)
        if not st_resolved:
            continue

        station_base = _station_base_from_resolved(st_resolved)
        key = station_bucket_key(station_base, rshort)
        if not key:
            continue

        states[key] = states.get(key, 0) + 1

    return states, by_route

# ---------- Demo main ----------
if __name__ == "__main__":
    # Simple loop to show it working; integrate this into your server loop/ESP bridge.
    while True:
        try:
            states, by_route = count_trains()

            total = sum(by_route.values())
            print(json.dumps({
                "feed_age_seconds": 0.0,  # you can fill with your cache age if you track it
                "total_trains": total,
                "by_route": by_route,
                "sample_of_states": dict(list(states.items())[:20])  # log a small slice
            }, ensure_ascii=False))

        except Exception as e:
            print(f"error: {e}")

        time.sleep(FEED_CACHE_SEC)
