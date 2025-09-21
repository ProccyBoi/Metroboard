# app.py
import os
import io
import zipfile
import asyncio
import csv
import logging
import json
import re
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

# Verbose logging flag
MB_VERBOSE = str(os.getenv("MB_VERBOSE", "0")).lower() not in ("0", "", "false", "no")

# Optional route-id -> short-name overrides from env
try:
    ROUTE_SHORT_OVERRIDES = json.loads(os.getenv("ROUTE_SHORT_OVERRIDES_JSON", "{}"))
except json.JSONDecodeError:
    ROUTE_SHORT_OVERRIDES = {}

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
        self.parent_to_name: Dict[str, str] = {}
        try:
            self._load(gtfs_dir)
            self._build_name_parent_index()
            self.available = True
            log.info("[metroboard] static GTFS loaded from %s", gtfs_dir)
        except FileNotFoundError as e:
            log.warning("[metroboard] static GTFS missing (%s). Running in degraded mode.", e)

    def _ensure_dir_or_download(self, d: str):
        # If already present (top-level routes.txt), we're good.
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "routes.txt")):
            return
    
        os.makedirs(d, exist_ok=True)
        data: Optional[bytes] = None
    
        # Prefer GitHub API envs if provided (owner/repo/tag/asset)
        if GH_OWNER and GH_REPO and GH_TAG and GH_ASSET:
            log.info(
                "[metroboard] fetching GTFS via GitHub API owner=%s repo=%s tag=%s asset=%s",
                GH_OWNER, GH_REPO, GH_TAG, GH_ASSET
            )
            data = _download_release_asset_via_github(
                GH_OWNER, GH_REPO, GH_TAG, GH_ASSET, GITHUB_TOKEN
            )
    
        # Otherwise fall back to direct URL, optionally with token if it's a GitHub URL
        elif GTFS_STATIC_ZIP_URL:
            log.info("[metroboard] downloading GTFS static zip from %s ...", GTFS_STATIC_ZIP_URL)
            headers = {}
            if "github.com" in GTFS_STATIC_ZIP_URL.lower() and GITHUB_TOKEN:
                headers["Authorization"] = f"token {GITHUB_TOKEN}"
                headers["Accept"] = "application/octet-stream"
            resp = requests.get(GTFS_STATIC_ZIP_URL, headers=headers, timeout=300)
            resp.raise_for_status()
            data = resp.content
    
        # If we still have no data, just return (run in degraded mode)
        if not data:
            log.warning("[metroboard] no GTFS source configured; running in degraded mode.")
            return
    
        # Extract, then promote nested files if needed
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(d)
        self._promote_inner_gtfs_files(d)
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
            # remember name for this parent
            if parent not in self.parent_to_name:
                self.parent_to_name[parent] = s.name
        for name, parents in name_to_parents.items():
            # keep the “first” parent for legacy lookups
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

    @staticmethod
    def _base_name_from_token(tok: str) -> str:
        """
        Strip optional ' (12345)' and trailing line suffix (digits or 'M') from a token.
        e.g. 'ChatswoodM (211400)' -> 'Chatswood', 'Town Hall4' -> 'Town Hall'
        """
        t = tok.strip()
        # Remove ' (12345)' if present
        if "(" in t and t.endswith(")"):
            t = t[: t.rfind("(")].strip()
        # Remove trailing digits / 'M' suffix
        i = len(t) - 1
        while i >= 0 and (t[i].isdigit() or t[i].upper() == "M"):
            i -= 1
        return t[: i + 1].strip()

    def _counts_for_tokens_fallback(state: "LiveState", tokens: List[str]) -> Dict[str, int]:
        from collections import Counter
        bucket_counts: Counter[Tuple[str, str]] = Counter()
    
        # Build counts keyed by (parent_id, line)
        for t in state.trains:
            line = t.route_short  # normalized 'T#' or 'M'
            if not line:
                continue
            seen: Set[Tuple[str, str]] = set()
            for sid in t.upcoming_stop_ids[:40]:
                parent = state.gtfs.parent_of(sid)
                key = (parent, line)
                if key not in seen:
                    bucket_counts[key] += 1
                    seen.add(key)
    
        # Helper to strip suffix/id from token -> base name
        def _base_name_from_token(tok: str) -> str:
            t = tok.strip()
            if "(" in t and t.endswith(")"):
                t = t[: t.rfind("(")].strip()
            i = len(t) - 1
            while i >= 0 and (t[i].isdigit() or t[i].upper() == "M"):
                i -= 1
            return t[: i + 1].strip()
    
        # Parse token "Central4", "ChatswoodM (211400)"
        def _parse_only_token(token: str) -> Optional[Tuple[str, str]]:
            token = token.strip()
            if not token:
                return None
            explicit_id = None
            name_part = token
            if "(" in token and token.endswith(")"):
                left = token.rfind("(")
                maybe_id = token[left + 1 : -1].strip()
                if maybe_id.isdigit():
                    explicit_id = maybe_id
                    name_part = token[:left].strip()
            i = len(name_part) - 1
            while i >= 0 and (name_part[i].isdigit() or name_part[i].upper() == "M"):
                i -= 1
            base_name = name_part[: i + 1].strip()
            suffix_part = name_part[i + 1 :].strip()
            # line code
            s = suffix_part.upper()
            if s == "M":
                line_code = "M"
            elif s.startswith("T") and s[1:].isdigit():
                line_code = s
            elif s.isdigit():
                line_code = f"T{s}"
            else:
                return None
            # parent id
            if explicit_id:
                parent = explicit_id
            else:
                parent = state.gtfs.parent_by_name(base_name)
            if not parent:
                return None
            return (parent, line_code)
    
        out: Dict[str, int] = {}
        for tok in tokens:
            parsed = _parse_only_token(tok)
            if not parsed:
                out[tok] = 0
                continue
            parent, line = parsed
    
            # 1) Exact (parent_id, line)
            total = bucket_counts.get((parent, line), 0)
    
            # 2) Prefix match for degraded mode (e.g. '200060' vs '2000601')
            if total == 0:
                for (p, l), v in bucket_counts.items():
                    if l == line and (p == parent or p.startswith(parent)):
                        total += v
    
            # 3) Name+line fallback (heavy vs metro at same-named station)
            if total == 0:
                base_name = _base_name_from_token(tok).lower()
                for (p, l), v in bucket_counts.items():
                    if l != line:
                        continue
                    pname = state.gtfs.parent_to_name.get(p, "")
                    if pname and pname.lower() == base_name:
                        total += v
            out[tok] = total
        return out




# -----------------------------
# Helpers
# -----------------------------
def normalize_line_code(val: str) -> Optional[str]:
    if not val:
        return None
    s = val.strip().upper()
    # Metro
    if s in ("M", "M1", "METRO", "S_M1"):
        return "M"
    # T lines
    if s.startswith("T") and s[1:].isdigit():
        return f"T{int(s[1:])}"
    if s.isdigit():
        return f"T{int(s)}"
    # Collapse forms like S_T1, S_T1_MAIN, etc.
    if s.startswith("S_T"):
        tail = s[3:]
        # tail could be '1', '1_MAIN', etc. Pull leading digits.
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            return f"T{int(digits)}"
    return None

_line_pat = re.compile(r'(?:\b|_)(T)(\d)(?:\b|_)', re.IGNORECASE)

def infer_line_code_from_text(*texts: str) -> Optional[str]:
    """
    Look through arbitrary strings (route_id, trip_id, etc) and try to infer 'T#' or 'M'.
    """
    for t in texts:
        if not t:
            continue
        s = t.upper()
        # Metro
        if "S_M1" in s or " METRO" in s or s.endswith("M1") or s == "M1" or s == "M":
            return "M"
        # T-lines (first match wins)
        m = _line_pat.search(s)
        if m:
            return f"T{m.group(2)}"
        # Also handle prefixes like 'S_T3_MAIN'
        if s.startswith("S_T") and len(s) > 3 and s[3].isdigit():
            return f"T{int(s[3])}"
    return None

def route_short_from_ids(route_id: str, route_short: str, trip_id: Optional[str] = None) -> Optional[str]:
    """
    Choose a sensible short code from GTFS short name / overrides / heuristics, normalized to 'T#' or 'M'.
    """
    # 1) start from routes.txt short_name if present
    cand = (route_short or "").strip()

    # 2) overlay env overrides by exact route_id
    if route_id and route_id in ROUTE_SHORT_OVERRIDES:
        cand = ROUTE_SHORT_OVERRIDES[route_id]

    # 3) if still blank, try the route_id itself
    if not cand and route_id:
        cand = route_id

    short = normalize_line_code(cand)

    # 4) last-resort: regex through route_id/trip_id for embedded T#/M patterns
    if not short:
        short = infer_line_code_from_text(route_id, trip_id)

    return short



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
        r = gtfs.routes.get(route_id)
        short_raw = r.short if r else ""
        short = route_short_from_ids(route_id, short_raw, trip_id=trip_id)
        stops = upcoming.get(trip_id, [])
        if not stops or not short:
            continue
        trains.append(TrainObs(
            trip_id=trip_id,
            route_id=route_id,
            route_short=short,       # normalized: 'T#' or 'M'
            upcoming_stop_ids=stops
        ))
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

            if MB_VERBOSE:
                log.info("[debug] upcoming dict sizes: trains=%d metro=%d", len(up_trains), len(up_metro))

            
            trains_obs = build_train_obs(state.gtfs, pairs_trains, up_trains)
            metro_obs  = build_train_obs(state.gtfs, pairs_metro,  up_metro)

            state.set_trains(trains_obs + metro_obs)
            if MB_VERBOSE:
                sample = state.trains[:10]
                for t in sample:
                    log.info(
                        "[debug] trip=%s route_id=%s short=%s first_stops=%s",
                        t.trip_id,
                        t.route_id,
                        t.route_short,
                        t.upcoming_stop_ids[:5],
                    )
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
    log.info("has counts_for_tokens? %s", hasattr(state, "counts_for_tokens"))

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
    if hasattr(state, "counts_for_tokens"):
        counts = state.counts_for_tokens(tokens)  # preferred
    else:
        log.warning("counts_for_tokens missing on LiveState; using fallback")
        counts = _counts_for_tokens_fallback(state, tokens)
    return JSONResponse(counts)


# run: uvicorn app:app --host 0.0.0.0 --port 8000
