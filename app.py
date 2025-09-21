# app.py
import os
import asyncio
import csv
import logging
from collections import defaultdict, Counter
from dataclasses import dataclass
from typing import Dict, Tuple, List, Optional, Set

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

# -----------------------------
# Config
# -----------------------------
TFNSW_API_KEY = os.getenv("TFNSW_API_KEY", "")
GTFS_DIR = os.getenv("GTFS_DIR", "./gtfs")  # folder with routes.txt, trips.txt, stops.txt, stop_times.txt
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "15"))

# NSW endpoints (Sydney Trains + Metro)
ENDPOINTS = {
    "vp_trains": "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
    "tu_trains": "https://api.transport.nsw.gov.au/v2/gtfs/realtime/sydneytrains",
    "vp_metro": "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
    "tu_metro": "https://api.transport.nsw.gov.au/v2/gtfs/realtime/metro",
}

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("metroboard")

# -----------------------------
# Data models (minimal)
# -----------------------------
@dataclass(frozen=True)
class Route:
    route_id: str
    short: str  # e.g. "T1", "T9", "M", etc.

@dataclass(frozen=True)
class Trip:
    trip_id: str
    route_id: str

@dataclass(frozen=True)
class Stop:
    stop_id: str
    name: str
    parent_station: Optional[str]  # parent stop_id or None

@dataclass
class TrainObs:
    # an observation for a single train (from TU join)
    trip_id: str
    route_id: str
    route_short: str  # "T1".."T9" or "M" etc.
    upcoming_stop_ids: List[str]  # future stops (stop_ids) if available; fallback to current/next stop
    # you can extend this with delay, position, etc.


# -----------------------------
# Static GTFS load
# -----------------------------
class StaticGTFS:
    def __init__(self, gtfs_dir: str):
        self.routes: Dict[str, Route] = {}
        self.trips: Dict[str, Trip] = {}
        self.stops: Dict[str, Stop] = {}

        # reverse maps
        self.stopname_to_parentid: Dict[str, str] = {}

        self._load(gtfs_dir)
        self._build_name_parent_index()

    def _load(self, d: str):
        # routes
        with open(os.path.join(d, "routes.txt"), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rid = row["route_id"]
                short = (row.get("route_short_name") or "").strip()
                self.routes[rid] = Route(route_id=rid, short=short)

        # trips
        with open(os.path.join(d, "trips.txt"), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                self.trips[row["trip_id"]] = Trip(trip_id=row["trip_id"], route_id=row["route_id"])

        # stops
        with open(os.path.join(d, "stops.txt"), newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                parent = row.get("parent_station") or None
                self.stops[row["stop_id"]] = Stop(
                    stop_id=row["stop_id"],
                    name=(row.get("stop_name") or "").strip(),
                    parent_station=parent if parent and parent != "" else None,
                )

    def _build_name_parent_index(self):
        # Map a display name (e.g. "Central") → a canonical parent_station_id
        # Preference: a stop that is a parent itself; otherwise the parent of children.
        name_to_parent_candidates: Dict[str, Set[str]] = defaultdict(set)
        for s in self.stops.values():
            parent = s.parent_station or s.stop_id
            name_to_parent_candidates[s.name].add(parent)

        for name, parents in name_to_parent_candidates.items():
            # Choose deterministically: smallest string id works fine for stability
            parent_id = sorted(parents)[0]
            self.stopname_to_parentid[name] = parent_id

    def parent_of(self, stop_id: str) -> str:
        """Return the parent station_id (or itself if none)."""
        s = self.stops.get(stop_id)
        if not s:
            return stop_id
        return s.parent_station or s.stop_id

    def parent_by_name(self, name: str) -> Optional[str]:
        return self.stopname_to_parentid.get(name)


# -----------------------------
# Live state cache
# -----------------------------
class LiveState:
    def __init__(self, gtfs: StaticGTFS):
        self.gtfs = gtfs
        self.trains: List[TrainObs] = []  # refreshed periodically

    def set_trains(self, trains: List[TrainObs]):
        self.trains = trains

    @staticmethod
    def _suffix_to_line_code(suffix: str) -> Optional[str]:
        # "1" -> "T1", ..., "9" -> "T9"; "M" -> "M"
        suffix = suffix.strip()
        if suffix.upper() == "M":
            return "M"
        if suffix.isdigit():
            return f"T{suffix}"
        # optionally support "T8" directly
        if suffix.upper().startswith("T") and suffix[1:].isdigit():
            return suffix.upper()
        return None

    def _parse_only_token(self, token: str) -> Optional[Tuple[str, str]]:
        """
        "Central9" -> (parent_station_id_for("Central"), "T9")
        "CentralM" -> (parent_station_id_for("Central"), "M")
        Also supports "Central (12345)9" forms, where name may carry a code in parentheses.
        """
        token = token.strip()
        if not token:
            return None

        # split trailing line suffix (digits or 'M')
        # find the last contiguous [0-9M]+ at end
        i = len(token) - 1
        while i >= 0 and (token[i].isdigit() or token[i].upper() == "M"):
            i -= 1
        name_part = token[: i + 1].strip()
        suffix_part = token[i + 1 :].strip()

        line_code = self._suffix_to_line_code(suffix_part)
        if not line_code:
            return None

        # strip any trailing station code in parentheses from the name when resolving
        # e.g. "Central (200060)" → "Central"
        display_name = name_part
        if "(" in name_part and name_part.endswith(")"):
            display_name = name_part[: name_part.rfind("(")].strip()

        parent = self.gtfs.parent_by_name(display_name)
        if not parent:
            return None

        return (parent, line_code)

    def counts_for_tokens(self, tokens: List[str]) -> Dict[str, int]:
        """
        For each token like "Central9" compute number of trains whose
        (parent_station, line_code) matches, based on *train.route_short*.
        """
        # bucket trains by (parent, line_code)
        bucket_counts: Counter[Tuple[str, str]] = Counter()

        for t in self.trains:
            line = normalize_line_code(t.route_short)
            if not line:
                continue
            # parentize all upcoming stops (first N to avoid explosion)
            seen: Set[Tuple[str, str]] = set()
            for sid in t.upcoming_stop_ids[:40]:
                parent = self.gtfs.parent_of(sid)
                key = (parent, line)
                if key not in seen:
                    bucket_counts[key] += 1
                    seen.add(key)

        # answer
        out: Dict[str, int] = {}
        for tok in tokens:
            parsed = self._parse_only_token(tok)
            if not parsed:
                out[tok] = 0
            else:
                out[tok] = bucket_counts.get(parsed, 0)
        return out


# -----------------------------
# Helpers
# -----------------------------
def normalize_line_code(short_name: str) -> Optional[str]:
    """
    Normalize route_short_name into "T1".."T9" or "M".
    """
    if not short_name:
        return None
    s = short_name.strip().upper()
    if s == "M":
        return "M"
    if s.startswith("T") and s[1:].isdigit():
        return s
    # Some feeds might use just digits for T lines
    if s.isdigit():
        return f"T{s}"
    return None


# -----------------------------
# Realtime fetch/join (minimal viable)
# -----------------------------
async def fetch_json(client: httpx.AsyncClient, url: str) -> dict:
    headers = {"Authorization": f"apikey {TFNSW_API_KEY}"} if TFNSW_API_KEY else {}
    r = await client.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {}

def join_tu_with_static(gtfs: StaticGTFS, tu_feed: dict, which: str) -> List[Tuple[str, str]]:
    """
    Return list of (trip_id, route_id) visible in TU feed.
    This is deliberately simplified; adapt to your TU schema.
    """
    out = []
    # TU is protobuf in reality; NSW API returns JSON wrapper.
    # Expect structure like: { "entity": [ { "trip_update": { "trip": { "trip_id": "...", "route_id": "..." }, ... } } ] }
    for ent in tu_feed.get("entity", []):
        tu = ent.get("trip_update")
        if not tu:
            continue
        trip = tu.get("trip", {})
        trip_id = trip.get("trip_id")
        route_id = trip.get("route_id")
        # if route_id is missing, get from static trips
        if not route_id and trip_id and trip_id in gtfs.trips:
            route_id = gtfs.trips[trip_id].route_id
        if trip_id and route_id:
            out.append((trip_id, route_id))
    log.info("[metroboard] collect_trips %s kept=%d", which, len(out))
    return out

def extract_upcoming_stops_from_tu(tu_feed: dict) -> Dict[str, List[str]]:
    """
    Map trip_id -> ordered list of upcoming stop_ids.
    """
    trip_to_stops: Dict[str, List[str]] = {}
    for ent in tu_feed.get("entity", []):
        tu = ent.get("trip_update")
        if not tu:
            continue
        trip = tu.get("trip", {})
        trip_id = trip.get("trip_id")
        if not trip_id:
            continue
        stops = []
        for stu in tu.get("stop_time_update", []):
            sid = stu.get("stop_id")
            if sid:
                stops.append(sid)
        if stops:
            trip_to_stops[trip_id] = stops
    return trip_to_stops

def build_train_obs(gtfs: StaticGTFS, pairs: List[Tuple[str, str]], upcoming: Dict[str, List[str]]) -> List[TrainObs]:
    trains: List[TrainObs] = []
    for trip_id, route_id in pairs:
        route = gtfs.routes.get(route_id)
        short = route.short if route else ""
        stops = upcoming.get(trip_id, [])
        if not stops:
            # Fallback: if no TU stop list, we can’t bucket well. Skip or keep empty list.
            continue
        trains.append(TrainObs(trip_id=trip_id, route_id=route_id, route_short=short, upcoming_stop_ids=stops))
    return trains

async def refresh_loop(state: LiveState):
    if not TFNSW_API_KEY:
        log.warning("TFNSW_API_KEY not set; realtime fetch will fail.")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                # Fetch both Sydney Trains + Metro TU
                tu_trains = await fetch_json(client, ENDPOINTS["tu_trains"])
                tu_metro = await fetch_json(client, ENDPOINTS["tu_metro"])

                # Join TU with static
                pairs_trains = join_tu_with_static(state.gtfs, tu_trains, "trains")
                pairs_metro = join_tu_with_static(state.gtfs, tu_metro, "metro")

                # Upcoming stop_ids
                up_trains = extract_upcoming_stops_from_tu(tu_trains)
                up_metro = extract_upcoming_stops_from_tu(tu_metro)

                trains_obs = build_train_obs(state.gtfs, pairs_trains, up_trains)
                metro_obs = build_train_obs(state.gtfs, pairs_metro, up_metro)

                all_obs = trains_obs + metro_obs
                state.set_trains(all_obs)

                log.info(
                    "[metroboard] processing trains=%d stations=%d prefer_tu=True",
                    len(all_obs),
                    len(state.gtfs.stops),
                )
            except Exception as e:
                log.exception("refresh_loop error: %s", e)
            await asyncio.sleep(POLL_SECONDS)


# -----------------------------
# FastAPI
# -----------------------------
gtfs = StaticGTFS(GTFS_DIR)
state = LiveState(gtfs)

app = FastAPI(title="Metroboard (line-separated)")

@app.on_event("startup")
async def _startup():
    asyncio.create_task(refresh_loop(state))
    log.info("Application startup complete.")

@app.get("/healthz")
async def health():
    return {"ok": True, "trains_cached": len(state.trains)}

@app.get("/states")
async def get_states(only: Optional[str] = Query(default=None)):
    """
    /states?only=Central4,Hornsby1,St%20MarysM
    Returns a dict { token: count }
    """
    if not only:
        # If no filter is supplied, return counts for *all* parent/line combos we currently know.
        # This avoids huge payloads by returning just a compact summary.
        # Aggregate a light summary per parent name: { "Central1": 5, "Central2": 3, ... }
        # For brevity, return empty unless explicitly requested:
        all_counts: Dict[str, int] = {}
        return JSONResponse(all_counts)

    tokens = [t for t in only.split(",") if t.strip()]
    counts = state.counts_for_tokens(tokens)
    return JSONResponse(counts)


# -----------------------------
# Run (for local dev)
# -----------------------------
# uvicorn app:app --host 0.0.0.0 --port 8000
