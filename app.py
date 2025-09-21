# app.py
import os
import io
import zipfile
import asyncio
import csv
import logging
from collections import defaultdict, Counter
from dataclasses import dataclass
from typing import Dict, Tuple, List, Optional, Set
import shutil
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

# -----------------------------
# Config
# -----------------------------
TFNSW_API_KEY = os.getenv("TFNSW_API_KEY", "")
GTFS_DIR = os.getenv("GTFS_DIR", "./gtfs")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "15"))
GTFS_STATIC_ZIP_URL = os.getenv("GTFS_STATIC_ZIP_URL", "")  # optional: if set, will download & unpack on first start
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
# GitHub release download (API path)
GH_OWNER = os.getenv("GTFS_GH_OWNER", "")
GH_REPO  = os.getenv("GTFS_GH_REPO", "")
GH_TAG   = os.getenv("GTFS_GH_TAG", "")
GH_ASSET = os.getenv("GTFS_GH_ASSET", "")


ENDPOINTS = {
    "tu_trains": "https://api.transport.nsw.gov.au/v2/gtfs/realtime/sydneytrains",
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
def _download_release_asset_via_github(owner: str, repo: str, tag: str, asset_name: str, token: str) -> bytes:
    """Fetch a release asset by name via the GitHub API (auth, no S3 redirect)."""
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set but GitHub release download was requested")
    api_base = f"https://api.github.com/repos/{owner}/{repo}"
    headers_json = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    # 1) find the release by tag
    rel = requests.get(f"{api_base}/releases/tags/{tag}", headers=headers_json, timeout=60)
    rel.raise_for_status()
    data = rel.json()
    asset = next((a for a in data.get("assets", []) if a.get("name") == asset_name), None)
    if not asset:
        raise RuntimeError(f"Asset '{asset_name}' not found on release tag '{tag}'")
    asset_id = asset["id"]
    # 2) download the asset content (no redirect, content streamed)
    headers_bin = {"Authorization": f"token {token}", "Accept": "application/octet-stream"}
    dl = requests.get(f"{api_base}/releases/assets/{asset_id}", headers=headers_bin, timeout=300)
    dl.raise_for_status()
    return dl.content


class StaticGTFS:
    def __init__(self, gtfs_dir: str):
        self.routes: Dict[str, Route] = {}
        self.trips: Dict[str, Trip] = {}
        self.stops: Dict[str, Stop] = {}
        self.stopname_to_parentid: Dict[str, str] = {}
        self.available: bool = False
        self._ensure_dir_or_download(gtfs_dir)
        try:
            self._load(gtfs_dir)
            self._build_name_parent_index()
            self.available = True
            log.info("[metroboard] static GTFS loaded from %s", gtfs_dir)
        except FileNotFoundError as e:
            log.warning("[metroboard] static GTFS missing (%s). Running in degraded mode.", e)

    def _ensure_dir_or_download(self, d: str):
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "routes.txt")):
            return
        if GTFS_STATIC_ZIP_URL or (os.getenv("GTFS_GH_OWNER") and os.getenv("GTFS_GH_REPO")):
            os.makedirs(d, exist_ok=True)
            # ... your current download code ...
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                z.extractall(d)
            self._promote_inner_gtfs_files(d)   # <-- add this line
            log.info("[metroboard] GTFS static extracted to %s", d)

    def _promote_inner_gtfs_files(self, d: str):
        """
        If routes.txt / trips.txt / stops.txt live in a single nested folder,
        move them up to `d` so the loader can find them.
        """
        needed = {"routes.txt", "trips.txt", "stops.txt"}
        root = Path(d)

        # Already fine?
        if all((root / f).exists() for f in needed):
            return

        # Search for first subdir that contains all needed files
        candidates = [p for p in root.rglob("*") if p.is_dir()]
        for cand in sorted(candidates, key=lambda p: len(p.parts)):  # shallow first
            if all((cand / f).exists() for f in needed):
                for f in needed:
                    src = cand / f
                    dst = root / f
                    if not dst.exists():
                        shutil.move(str(src), str(dst))
                return

    def _load(self, d: str):
        def open_csv(path):
            if not os.path.exists(path):
                raise FileNotFoundError(f"Missing {os.path.basename(path)} in {d}")
            # utf-8-sig strips a possible BOM so 'stop_id' isn't read as '\ufeffstop_id'
            return open(path, newline="", encoding="utf-8-sig")
    
        # routes.txt
        with open_csv(os.path.join(d, "routes.txt")) as f:
            rdr = csv.DictReader(f)
            if not rdr.fieldnames:
                raise ValueError("routes.txt has no header row")
            for row in rdr:
                rid = row.get("route_id", "").strip()
                short = (row.get("route_short_name") or "").strip()
                if rid:
                    self.routes[rid] = Route(route_id=rid, short=short)
    
        # trips.txt
        with open_csv(os.path.join(d, "trips.txt")) as f:
            rdr = csv.DictReader(f)
            if not rdr.fieldnames:
                raise ValueError("trips.txt has no header row")
            for row in rdr:
                tid = row.get("trip_id", "").strip()
                rid = row.get("route_id", "").strip()
                if tid and rid:
                    self.trips[tid] = Trip(trip_id=tid, route_id=rid)
    
        # stops.txt
        with open_csv(os.path.join(d, "stops.txt")) as f:
            rdr = csv.DictReader(f)
            if not rdr.fieldnames or "stop_id" not in [h.strip().lstrip("\ufeff") for h in rdr.fieldnames]:
                raise ValueError(f"stops.txt missing 'stop_id' header; got {rdr.fieldnames}")
            for row in rdr:
                sid = (row.get("stop_id") or row.get("\ufeffstop_id") or "").strip()
                name = (row.get("stop_name") or "").strip()
                parent = (row.get("parent_station") or "").strip() or None
                if sid:
                    self.stops[sid] = Stop(stop_id=sid, name=name, parent_station=parent)


    def _build_name_parent_index(self):
        name_to_parents: Dict[str, Set[str]] = defaultdict(set)
        for s in self.stops.values():
            parent = s.parent_station or s.stop_id
            name_to_parents[s.name].add(parent)
        for name, parents in name_to_parents.items():
            self.stopname_to_parentid[name] = sorted(parents)[0]

    def parent_of(self, stop_id: str) -> str:
        """
        Returns a 'parent station' key for a given stop_id. If static GTFS is missing,
        try to coerce a stable parent by stripping to the leading digits (e.g., '2000601' -> '200060').
        Otherwise, fall back to the stop_id itself.
        """
        s = self.stops.get(stop_id)
        if s:
            return s.parent_station or s.stop_id
        # degraded mode heuristics
        digits = ""
        for ch in stop_id:
            if ch.isdigit():
                digits += ch
            else:
                break
        return digits if digits else stop_id

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
        """
        Supports:
          - 'Central4' / 'CentralM'
          - 'CentralM (200060)'  -> uses 200060 directly as the parent key
        """
        token = token.strip()
        if not token:
            return None

        # extract optional ' (12345)' id
        explicit_id: Optional[str] = None
        name_part = token
        if "(" in token and token.endswith(")"):
            # e.g., 'CentralM (200060)'
            left = token.rfind("(")
            maybe_id = token[left + 1 : -1].strip()
            if maybe_id.isdigit():
                explicit_id = maybe_id
                name_part = token[:left].strip()

        # split suffix: 'Central4' -> name='Central', suffix='4'
        i = len(name_part) - 1
        while i >= 0 and (name_part[i].isdigit() or name_part[i].upper() == "M"):
            i -= 1
        base_name = name_part[: i + 1].strip()
        suffix_part = name_part[i + 1 :].strip()
        line_code = self._suffix_to_line_code(suffix_part)
        if not line_code:
            return None

        parent: Optional[str] = None
        if explicit_id:
            parent = explicit_id
        else:
            parent = self.gtfs.parent_by_name(base_name)

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
            if parsed:
                parent, line = parsed
                # also count stops whose IDs start with the parent (helps in degraded mode: '200060' vs '2000601')
                total = bucket_counts.get(parsed, 0)
                if total == 0:
                    # try prefix match in degraded mode
                    for (p, l), v in bucket_counts.items():
                        if l == line and (p == parent or p.startswith(parent)):
                            total += v
                out[tok] = total
            else:
                out[tok] = 0
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
# GTFS-RT fetch + parse (requests + protobuf)
# -----------------------------
def _fetch_bytes(url: str) -> bytes:
    headers = {"Authorization": f"apikey {TFNSW_API_KEY}"} if TFNSW_API_KEY else {}
    headers["Accept"] = "application/x-google-protobuf"
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.content

def fetch_tu_feed(url: str) -> gtfs_realtime_pb2.FeedMessage:
    raw = _fetch_bytes(url)
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(raw)
    return feed

def join_tu_with_static(gtfs: StaticGTFS, feed: gtfs_realtime_pb2.FeedMessage, label: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    kept = 0
    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        trip_id = tu.trip.trip_id
        route_id = tu.trip.route_id
        if not route_id and trip_id in gtfs.trips:
            route_id = gtfs.trips[trip_id].route_id
        if trip_id and route_id:
            out.append((trip_id, route_id))
            kept += 1
    log.info("[metroboard] collect_trips %s kept=%d", label, kept)
    return out

def extract_upcoming_stops_from_tu(feed: gtfs_realtime_pb2.FeedMessage) -> Dict[str, List[str]]:
    trip_to_stops: Dict[str, List[str]] = {}
    for ent in feed.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        trip_id = tu.trip.trip_id
        if not trip_id:
            continue
        stops = []
        for stu in tu.stop_time_update:
            sid = stu.stop_id
            if sid:
                stops.append(sid)
        if stops:
            trip_to_stops[trip_id] = stops
    return trip_to_stops

def build_train_obs(gtfs: StaticGTFS, pairs: List[Tuple[str, str]], upcoming: Dict[str, List[str]]) -> List[TrainObs]:
    trains: List[TrainObs] = []
    for trip_id, route_id in pairs:
        short = ""
        r = gtfs.routes.get(route_id)
        if r:
            short = r.short
        # In degraded mode (no routes.txt), try to infer 'M' vs 'T#' from route_id text as a fallback
        if not short and route_id:
            s = route_id.upper()
            if s == "M" or s.startswith("METRO"):
                short = "M"
            elif s.startswith("T") and any(ch.isdigit() for ch in s[1:2]):
                short = f"T{s[1:2]}"
        stops = upcoming.get(trip_id, [])
        if not stops:
            continue
        trains.append(TrainObs(trip_id=trip_id, route_id=route_id, route_short=short, upcoming_stop_ids=stops))
    return trains

# -----------------------------
# Background refresh loop
# -----------------------------
async def refresh_loop(state: LiveState):
    if not TFNSW_API_KEY:
        log.warning("TFNSW_API_KEY not set; realtime fetch will fail.")

    while True:
        try:
            tu_trains, tu_metro = await asyncio.gather(
                asyncio.to_thread(fetch_tu_feed, ENDPOINTS["tu_trains"]),
                asyncio.to_thread(fetch_tu_feed, ENDPOINTS["tu_metro"]),
            )

            pairs_trains = join_tu_with_static(state.gtfs, tu_trains, "trains")
            pairs_metro  = join_tu_with_static(state.gtfs, tu_metro,  "metro")

            up_trains = extract_upcoming_stops_from_tu(tu_trains)
            up_metro  = extract_upcoming_stops_from_tu(tu_metro)

            trains_obs = build_train_obs(state.gtfs, pairs_trains, up_trains)
            metro_obs  = build_train_obs(state.gtfs, pairs_metro,  up_metro)

            state.set_trains(trains_obs + metro_obs)
            log.info(
                "[metroboard] processing trains=%d stations=%d static_available=%s",
                len(state.trains),
                len(state.gtfs.stops) if state.gtfs.available else 0,
                state.gtfs.available,
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
    return {
        "ok": True,
        "trains_cached": len(state.trains),
        "static_gtfs_available": state.gtfs.available,
        "gtfs_dir": GTFS_DIR,
    }

@app.get("/states")
async def get_states(only: Optional[str] = Query(default=None)):
    if not only:
        return JSONResponse({})
    tokens = [t for t in only.split(",") if t.strip()]
    counts = state.counts_for_tokens(tokens)
    return JSONResponse(counts)

# run: uvicorn app:app --host 0.0.0.0 --port 8000
