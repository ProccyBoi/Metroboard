# app.py
import os
import asyncio
import csv
import json
import logging
from collections import defaultdict, Counter
from dataclasses import dataclass
from typing import Dict, Tuple, List, Optional, Set

# --- optional httpx; we fall back to stdlib if it isn't installed
try:
    import httpx  # type: ignore
except Exception:
    httpx = None  # noqa: N816

import urllib.request
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

# -----------------------------
# Config
# -----------------------------
TFNSW_API_KEY = os.getenv("TFNSW_API_KEY", "")
GTFS_DIR = os.getenv("GTFS_DIR", "./gtfs")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "15"))

ENDPOINTS = {
    "vp_trains": "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
    "tu_trains": "https://api.transport.nsw.gov.au/v2/gtfs/realtime/sydneytrains",
    "vp_metro":  "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
    "tu_metro":  "https://api.transport.nsw.gov.au/v2/gtfs/realtime/metro",
}

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("metroboard")

# -----------------------------
# Data models
# -----------------------------
@dataclass(frozen=True)
class Route:
    route_id: str
    short: str  # "T1".."T9" or "M" etc.

@dataclass(frozen=True)
class Trip:
    trip_id: str
    route_id: str

@dataclass(frozen=True)
class Stop:
    stop_id: str
    name: str
    parent_station: Optional[str]

@dataclass
class TrainObs:
    trip_id: str
    route_id: str
    route_short: str
    upcoming_stop_ids: List[str]

# -----------------------------
# Static GTFS
# -----------------------------
class StaticGTFS:
    def __init__(self, gtfs_dir: str):
        self.routes: Dict[str, Route] = {}
        self.trips: Dict[str, Trip] = {}
        self.stops: Dict[str, Stop] = {}
        self.stopname_to_parentid: Dict[str, str] = {}
        self._load(gtfs_dir)
        self._build_name_parent_index()

    def _load(self, d: str):
        def open_csv(path):
            if not os.path.exists(path):
                raise FileNotFoundError(f"Missing {os.path.basename(path)} in {d}")
            return open(path, newline="", encoding="utf-8")

        with open_csv(os.path.join(d, "routes.txt")) as f:
            for row in csv.DictReader(f):
                rid = row["route_id"]
                short = (row.get("route_short_name") or "").strip()
                self.routes[rid] = Route(route_id=rid, short=short)

        with open_csv(os.path.join(d, "trips.txt")) as f:
            for row in csv.DictReader(f):
                self.trips[row["trip_id"]] = Trip(trip_id=row["trip_id"], route_id=row["route_id"])

        with open_csv(os.path.join(d, "stops.txt")) as f:
            for row in csv.DictReader(f):
                parent = row.get("parent_station") or None
                self.stops[row["stop_id"]] = Stop(
                    stop_id=row["stop_id"],
                    name=(row.get("stop_name") or "").strip(),
                    parent_station=parent if parent else None,
                )

    def _build_name_parent_index(self):
        name_to_parents: Dict[str, Set[str]] = defaultdict(set)
        for s in self.stops.values():
            parent = s.parent_station or s.stop_id
            name_to_parents[s.name].add(parent)
        for name, parents in name_to_parents.items():
            self.stopname_to_parentid[name] = sorted(parents)[0]

    def parent_of(self, stop_id: str) -> str:
        s = self.stops.get(stop_id)
        if not s:
            return stop_id
        return s.parent_station or s.stop_id

    def parent_by_name(self, name: str) -> Optional[str]:
        return self.stopname_to_parentid.get(name)

# -----------------------------
# Live state
# -----------------------------
class LiveState:
    def __init__(self, gtfs: StaticGTFS):
        self.gtfs = gtfs
        self.trains: List[TrainObs] = []

    def set_trains(self, trains: List[TrainObs]):
        self.trains = trains

    @staticmethod
    def _suffix_to_line_code(suffix: str) -> Optional[str]:
        s = suffix.strip().upper()
        if s == "M":
            return "M"
        if s.startswith("T") and s[1:].isdigit():
            return s
        if s.isdigit():
            return f"T{s}"
        return None

    def _parse_only_token(self, token: str) -> Optional[Tuple[str, str]]:
        token = token.strip()
        if not token:
            return None

        i = len(token) - 1
        while i >= 0 and (token[i].isdigit() or token[i].upper() == "M"):
            i -= 1
        name_part = token[: i + 1].strip()
        suffix_part = token[i + 1 :].strip()
        line_code = self._suffix_to_line_code(suffix_part)
        if not line_code:
            return None

        # remove trailing "(12345)" if present
        if "(" in name_part and name_part.endswith(")"):
            name_part = name_part[: name_part.rfind("(")].strip()

        parent = self.gtfs.parent_by_name(name_part)
        if not parent:
            return None
        return (parent, line_code)

    def counts_for_tokens(self, tokens: List[str]) -> Dict[str, int]:
        bucket_counts: Counter[Tuple[str, str]] = Counter()

        for t in self.trains:
            line = normalize_line_code(t.route_short)
            if not line:
                continue
            seen: Set[Tuple[str, str]] = set()
            for sid in t.upcoming_stop_ids[:40]:
                parent = self.gtfs.parent_of(sid)
                key = (parent, line)
                if key not in seen:
                    bucket_counts[key] += 1
                    seen.add(key)

        out: Dict[str, int] = {}
        for tok in tokens:
            parsed = self._parse_only_token(tok)
            out[tok] = bucket_counts.get(parsed, 0) if parsed else 0
        return out

# -----------------------------
# Helpers
# -----------------------------
def normalize_line_code(short_name: str) -> Optional[str]:
    if not short_name:
        return None
    s = short_name.strip().upper()
    if s == "M":
        return "M"
    if s.startswith("T") and s[1:].isdigit():
        return s
    if s.isdigit():
        return f"T{s}"
    return None

# -----------------------------
# HTTP fetchers (httpx or stdlib)
# -----------------------------
async def fetch_json(url: str) -> dict:
    headers = {"Authorization": f"apikey {TFNSW_API_KEY}"} if TFNSW_API_KEY else {}

    if httpx:
        async with httpx.AsyncClient(timeout=20.0) as client:  # type: ignore
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return {}
    # stdlib fallback (non-async) → run in thread
    def _sync_get():
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}
    return await asyncio.to_thread(_sync_get)

def join_tu_with_static(gtfs: StaticGTFS, tu_feed: dict, label: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for ent in tu_feed.get("entity", []):
        tu = ent.get("trip_update")
        if not tu:
            continue
        trip = tu.get("trip", {})
        trip_id = trip.get("trip_id")
        route_id = trip.get("route_id")
        if not route_id and trip_id and trip_id in gtfs.trips:
            route_id = gtfs.trips[trip_id].route_id
        if trip_id and route_id:
            out.append((trip_id, route_id))
    log.info("[metroboard] collect_trips %s kept=%d", label, len(out))
    return out

def extract_upcoming_stops_from_tu(tu_feed: dict) -> Dict[str, List[str]]:
    trip_to_stops: Dict[str, List[str]] = {}
    for ent in tu_feed.get("entity", []):
        tu = ent.get("trip_update")
        if not tu:
            continue
        trip_id = tu.get("trip", {}).get("trip_id")
        if not trip_id:
            continue
        stops = [stu.get("stop_id") for stu in tu.get("stop_time_update", []) if stu.get("stop_id")]
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
            continue
        trains.append(TrainObs(trip_id=trip_id, route_id=route_id, route_short=short, upcoming_stop_ids=stops))
    return trains

async def refresh_loop(state: LiveState):
    if not TFNSW_API_KEY:
        log.warning("TFNSW_API_KEY not set; realtime fetch will fail.")

    while True:
        try:
            tu_trains = await fetch_json(ENDPOINTS["tu_trains"])
            tu_metro  = await fetch_json(ENDPOINTS["tu_metro"])

            pairs_trains = join_tu_with_static(state.gtfs, tu_trains, "trains")
            pairs_metro  = join_tu_with_static(state.gtfs, tu_metro,  "metro")

            up_trains = extract_upcoming_stops_from_tu(tu_trains)
            up_metro  = extract_upcoming_stops_from_tu(tu_metro)

            trains_obs = build_train_obs(state.gtfs, pairs_trains, up_trains)
            metro_obs  = build_train_obs(state.gtfs, pairs_metro,  up_metro)

            state.set_trains(trains_obs + metro_obs)
            log.info(
                "[metroboard] processing trains=%d stations=%d prefer_tu=True",
                len(state.trains), len(state.gtfs.stops),
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
    if not only:
        return JSONResponse({})
    tokens = [t for t in only.split(",") if t.strip()]
    counts = state.counts_for_tokens(tokens)
    return JSONResponse(counts)

# uvicorn app:app --host 0.0.0.0 --port 8000
