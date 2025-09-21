# app.py — Metroboard backend (Rail + Metro only, strict parents)
# - Requires static GTFS files: routes.txt, trips.txt, stops.txt, stop_times.txt, agency.txt
# - Builds an "allowed parent station" set from routes with route_type in {1 (subway), 2 (rail)}
#   optionally filtered by an agency allow-list (e.g. Sydney Trains, NSW Trains, Sydney Metro).
# - Buckets ONLY stops whose parent is in the allowed set (filters out bus/light rail/wharf/etc).
# - Supports route short-name overrides and line aliases (e.g. merge T6 into T4 if desired).
# - Endpoints: /healthz, /stations (debug), /states?only=Central4,ChatswoodM,...

import os, io, zipfile, asyncio, csv, logging, json, re, shutil
from collections import defaultdict, Counter
from dataclasses import dataclass
from typing import Dict, Tuple, List, Optional, Set
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

# -----------------------------
# Config (env)
# -----------------------------
TFNSW_API_KEY = os.getenv("TFNSW_API_KEY", "")
GTFS_DIR = os.getenv("GTFS_DIR", "./gtfs")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "15"))

# GTFS static zip (optional) — either direct URL or GitHub release asset
GTFS_STATIC_ZIP_URL = os.getenv("GTFS_STATIC_ZIP_URL", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GH_OWNER = os.getenv("GTFS_GH_OWNER", "")
GH_REPO  = os.getenv("GTFS_GH_REPO", "")
GH_TAG   = os.getenv("GTFS_GH_TAG", "")
GH_ASSET = os.getenv("GTFS_GH_ASSET", "")

# Route-types to allow (default: 1=subway, 2=rail)
ROUTE_TYPES_ALLOW = {int(x) for x in os.getenv("ROUTE_TYPES_ALLOW", "1,2").split(",") if x.strip().isdigit()}

# Optional agency allow-list: comma-separated exact names (matches agency.txt agency_name)
AGENCY_ALLOW = {x.strip() for x in os.getenv("AGENCY_ALLOW", "Sydney Trains,NSW Trains,Sydney Metro").split(",") if x.strip()}

# Strict token-line matching (disables lenient fallbacks)
STRICT_LINE_MATCH = str(os.getenv("STRICT_LINE_MATCH", "1")).lower() in ("1","true","yes")

# Verbose logs
MB_VERBOSE = str(os.getenv("MB_VERBOSE", "0")).lower() not in ("0", "", "false", "no")

# Route short-name overrides (route_id -> "T#" or "M")
try:
    ROUTE_SHORT_OVERRIDES: Dict[str, str] = json.loads(os.getenv("ROUTE_SHORT_OVERRIDES_JSON", "{}"))
except json.JSONDecodeError:
    ROUTE_SHORT_OVERRIDES = {}

# Line aliases (e.g. {"T4":["T4","T6"]} to count T6 trains as T4)
try:
    LINE_ALIASES: Dict[str, List[str]] = json.loads(os.getenv("LINE_ALIASES_JSON", "{}"))
except json.JSONDecodeError:
    LINE_ALIASES = {}

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
    short: str
    route_type: Optional[int]
    agency_id: Optional[str]

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
    route_short: str  # 'T#' or 'M'
    upcoming_stop_ids: List[str]

# -----------------------------
# Static GTFS loader (with strict parent filtering)
# -----------------------------
def _download_release_asset_via_github(owner: str, repo: str, tag: str, asset_name: str, token: str) -> bytes:
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set but GitHub release download was requested")
    api_base = f"https://api.github.com/repos/{owner}/{repo}"
    headers_json = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    r = requests.get(f"{api_base}/releases/tags/{tag}", headers=headers_json, timeout=60)
    r.raise_for_status()
    data = r.json()
    asset = next((a for a in data.get("assets", []) if a.get("name") == asset_name), None)
    if not asset:
        raise RuntimeError(f"Asset '{asset_name}' not found on release tag '{tag}'")
    headers_bin = {"Authorization": f"token {token}", "Accept": "application/octet-stream"}
    dl = requests.get(f"{api_base}/releases/assets/{asset['id']}", headers=headers_bin, timeout=300)
    dl.raise_for_status()
    return dl.content

class StaticGTFS:
    def __init__(self, gtfs_dir: str):
        self.routes: Dict[str, Route] = {}
        self.trips: Dict[str, Trip] = {}
        self.stops: Dict[str, Stop] = {}         # all raw stops
        self.parent_to_name: Dict[str, str] = {} # parent -> canonical name
        self.stopname_to_parentid: Dict[str, str] = {}
        self._name_norm_to_parentid: Dict[str, str] = {}
        self.agency_id_to_name: Dict[str, str] = {}

        self.available: bool = False
        self.allowed_route_ids: Set[str] = set()
        self.allowed_parent_ids: Set[str] = set()

        self._ensure_dir_or_download(gtfs_dir)
        try:
            self._load_all(gtfs_dir)
            self._compute_allowed_sets()
            self._build_name_parent_index()  # only for allowed parents
            self.available = True
            log.info("[metroboard] static GTFS ready: routes=%d trips=%d stops=%d allowed_parents=%d",
                     len(self.routes), len(self.trips), len(self.stops), len(self.allowed_parent_ids))
        except Exception as e:
            log.exception("[metroboard] static GTFS load failed (degraded mode): %s", e)

    def _ensure_dir_or_download(self, d: str):
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "routes.txt")):
            return
        os.makedirs(d, exist_ok=True)
        data: Optional[bytes] = None

        if GH_OWNER and GH_REPO and GH_TAG and GH_ASSET:
            log.info("[metroboard] fetching GTFS via GitHub API %s/%s@%s asset=%s", GH_OWNER, GH_REPO, GH_TAG, GH_ASSET)
            data = _download_release_asset_via_github(GH_OWNER, GH_REPO, GH_TAG, GH_ASSET, GITHUB_TOKEN)
        elif GTFS_STATIC_ZIP_URL:
            log.info("[metroboard] downloading GTFS static zip from %s ...", GTFS_STATIC_ZIP_URL)
            headers = {}
            if "github.com" in GTFS_STATIC_ZIP_URL.lower() and GITHUB_TOKEN:
                headers["Authorization"] = f"token {GITHUB_TOKEN}"
                headers["Accept"] = "application/octet-stream"
            resp = requests.get(GTFS_STATIC_ZIP_URL, headers=headers, timeout=300)
            resp.raise_for_status()
            data = resp.content

        if not data:
            log.warning("[metroboard] no GTFS source configured; expecting files at %s", d)
            return

        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(d)
        self._promote_inner_gtfs_files(d)
        log.info("[metroboard] GTFS static extracted to %s", d)

    def _promote_inner_gtfs_files(self, d: str):
        needed = {"routes.txt", "trips.txt", "stops.txt", "stop_times.txt", "agency.txt"}
        root = Path(d)
        if all((root / f).exists() for f in needed):
            return
        for cand in sorted([p for p in root.rglob("*") if p.is_dir()], key=lambda p: len(p.parts)):
            if all((cand / f).exists() for f in needed):
                for f in needed:
                    if not (root / f).exists():
                        shutil.move(str(cand / f), str(root / f))
                return

    def _open_csv(self, d: str, fname: str):
        path = os.path.join(d, fname)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Missing {fname} in {d}")
        return open(path, newline="", encoding="utf-8-sig")

    def _load_all(self, d: str):
        # agency.txt (id -> name)
        with self._open_csv(d, "agency.txt") as f:
            rdr = csv.DictReader(f)
            for row in rdr:
                aid = (row.get("agency_id") or "").strip() or None
                aname = (row.get("agency_name") or "").strip()
                if aid:
                    self.agency_id_to_name[aid] = aname

        # routes.txt
        with self._open_csv(d, "routes.txt") as f:
            rdr = csv.DictReader(f)
            for row in rdr:
                rid = (row.get("route_id") or "").strip()
                short = (row.get("route_short_name") or "").strip()
                route_type = None
                try:
                    route_type = int(row.get("route_type", "").strip())
                except Exception:
                    pass
                agency_id = (row.get("agency_id") or "").strip() or None
                if rid:
                    self.routes[rid] = Route(rid, short, route_type, agency_id)

        # trips.txt
        with self._open_csv(d, "trips.txt") as f:
            rdr = csv.DictReader(f)
            for row in rdr:
                tid = (row.get("trip_id") or "").strip()
                rid = (row.get("route_id") or "").strip()
                if tid and rid:
                    self.trips[tid] = Trip(tid, rid)

        # stops.txt
        with self._open_csv(d, "stops.txt") as f:
            rdr = csv.DictReader(f)
            # tolerate BOM names
            headers = [h.strip().lstrip("\ufeff") for h in (rdr.fieldnames or [])]
            idx = {h: i for i, h in enumerate(headers)}
            for row in rdr:
                sid = (row.get("stop_id") or row.get("\ufeffstop_id") or "").strip()
                name = (row.get("stop_name") or "").strip()
                parent = (row.get("parent_station") or "").strip() or None
                if sid:
                    self.stops[sid] = Stop(sid, name, parent)

        # stop_times.txt: we don't store; we’ll scan it during _compute_allowed_sets
        self._stop_times_path = os.path.join(d, "stop_times.txt")

    def _route_allowed(self, r: Route) -> bool:
        if r.route_type not in ROUTE_TYPES_ALLOW:
            return False
        if AGENCY_ALLOW:
            aname = self.agency_id_to_name.get(r.agency_id or "", "")
            if aname and aname not in AGENCY_ALLOW:
                return False
        return True

    def _compute_allowed_sets(self):
        # 1) routes by type/agency
        for r in self.routes.values():
            if self._route_allowed(r):
                self.allowed_route_ids.add(r.route_id)

        if not self.allowed_route_ids:
            raise RuntimeError("No routes matched ROUTE_TYPES_ALLOW/AGENCY_ALLOW — abort to avoid polluted data.")

        # 2) allowed trips (by allowed routes)
        allowed_trips = {t.trip_id for t in self.trips.values() if t.route_id in self.allowed_route_ids}
        if not allowed_trips:
            raise RuntimeError("No trips for allowed routes.")

        # 3) allowed parent stations from stop_times of allowed trips
        parents: Set[str] = set()
        with self._open_csv(os.path.dirname(self._stop_times_path), "stop_times.txt") as f:
            rdr = csv.DictReader(f)
            for row in rdr:
                tid = (row.get("trip_id") or "").strip()
                if tid not in allowed_trips:
                    continue
                sid = (row.get("stop_id") or "").strip()
                if not sid:
                    continue
                parent = self.parent_of(sid)  # uses stops map; if no parent, returns sid
                parents.add(parent)

        if not parents:
            raise RuntimeError("No parent stations derived from allowed trips/stop_times.")

        self.allowed_parent_ids = parents

        # Build canonical parent->name map for allowed parents only
        self.parent_to_name.clear()
        for s in self.stops.values():
            parent = s.parent_station or s.stop_id
            if parent in self.allowed_parent_ids and parent not in self.parent_to_name:
                self.parent_to_name[parent] = s.name

    def _build_name_parent_index(self):
        # index only allowed parents
        name_to_parents: Dict[str, Set[str]] = defaultdict(set)
        for s in self.stops.values():
            parent = s.parent_station or s.stop_id
            if parent not in self.allowed_parent_ids:
                continue
            name_to_parents[s.name].add(parent)

        self.stopname_to_parentid.clear()
        self._name_norm_to_parentid.clear()
        for name, parents in name_to_parents.items():
            first_parent = sorted(parents)[0]
            self.stopname_to_parentid[name] = first_parent
            self._name_norm_to_parentid[self._norm_name(name)] = first_parent

    # ---- helpers
    def parent_of(self, stop_id: str) -> str:
        s = self.stops.get(stop_id)
        if s:
            return s.parent_station or s.stop_id
        # degraded fallback: strip leading digits
        digits = "".join(ch for ch in stop_id if ch.isdigit())
        return digits or stop_id

    def parent_by_name(self, name: str) -> Optional[str]:
        p = self.stopname_to_parentid.get(name)
        if p:
            return p
        return self._name_norm_to_parentid.get(self._norm_name(name))

    @staticmethod
    def _norm_name(s: str) -> str:
        s = s.strip().lower()
        for suf in (" station", " railway station"):
            if s.endswith(suf):
                s = s[: -len(suf)]
        s = s.replace("&", "and")
        s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
        s = " ".join(s.split())
        return s

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

    def counts_for_tokens(self, tokens: List[str]) -> Dict[str, int]:
        """
        Robust counting with both parent-id and normalized-name buckets.
        Critically, ONLY counts stops whose parent is in gtfs.allowed_parent_ids.
        """
        by_parent_line: Counter[Tuple[str, str]] = Counter()
        by_name_line:   Counter[Tuple[str, str]] = Counter()
        by_parent_only: Counter[str] = Counter()
        by_name_only:   Counter[str] = Counter()

        name_norm_by_parent: Dict[str, str] = {}

        # Build buckets (distinct parents per trip per line)
        for t in self.trains:
            line = t.route_short
            if not line:
                continue

            seen_parent_line: Set[Tuple[str, str]] = set()
            seen_name_line:   Set[Tuple[str, str]] = set()
            seen_parent:      Set[str] = set()
            seen_name:        Set[str] = set()

            for sid in t.upcoming_stop_ids:
                parent = self.gtfs.parent_of(sid)
                if parent not in self.gtfs.allowed_parent_ids:
                    continue  # HARD FILTER

                if parent not in name_norm_by_parent:
                    pname = self.gtfs.parent_to_name.get(parent) or ""
                    name_norm_by_parent[parent] = self.gtfs._norm_name(pname)
                name_norm = name_norm_by_parent[parent]

                kpl = (parent, line)
                if kpl not in seen_parent_line:
                    by_parent_line[kpl] += 1
                    seen_parent_line.add(kpl)

                knl = (name_norm, line)
                if name_norm and knl not in seen_name_line:
                    by_name_line[knl] += 1
                    seen_name_line.add(knl)

                if parent not in seen_parent:
                    by_parent_only[parent] += 1
                    seen_parent.add(parent)

                if name_norm and name_norm not in seen_name:
                    by_name_only[name_norm] += 1
                    seen_name.add(name_norm)

        if MB_VERBOSE:
            log.info("[states] buckets: parent_line=%d name_line=%d parent_only=%d name_only=%d",
                     len(by_parent_line), len(by_name_line), len(by_parent_only), len(by_name_only))
            line_hist = Counter([ln for (_, ln) in by_parent_line.keys()])
            log.info("[states] lines seen: %s", dict(line_hist.most_common(10)))
            sample_names = list(by_name_only.items())[:20]
            log.info("[states] sample names: %s", [(k, v) for k, v in sample_names])

        def parse_tok(token: str):
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

            line = self._suffix_to_line_code(suffix_part or "")
            if not line:
                return None

            parent = explicit_id if explicit_id else self.gtfs.parent_by_name(base_name)
            norm_name = self.gtfs._norm_name(base_name)
            return (parent, line, norm_name)

        out: Dict[str, int] = {}
        for tok in tokens:
            parsed = parse_tok(tok)
            if not parsed:
                out[tok] = 0
                continue

            parent, line, norm_name = parsed
            aliases = LINE_ALIASES.get(line, [line])

            total = 0
            if parent and parent in self.gtfs.allowed_parent_ids:
                total = sum(by_parent_line.get((parent, al), 0) for al in aliases)

            if not total and norm_name:
                total = sum(by_name_line.get((norm_name, al), 0) for al in aliases)

            if not total and not STRICT_LINE_MATCH and parent and parent in self.gtfs.allowed_parent_ids:
                total = max(total, by_parent_only.get(parent, 0))

            if not total and not STRICT_LINE_MATCH and norm_name:
                total = max(total, by_name_only.get(norm_name, 0))

            out[tok] = total

        return out

# -----------------------------
# Helpers
# -----------------------------
_line_pat = re.compile(r'(?:\b|_)(T)(\d)(?:\b|_)', re.IGNORECASE)

def normalize_line_code(val: str) -> Optional[str]:
    if not val:
        return None
    s = val.strip().upper()
    if s in ("M", "M1", "METRO", "S_M1"):
        return "M"
    if s.startswith("T") and s[1:].isdigit():
        return f"T{int(s[1:])}"
    if s.isdigit():
        return f"T{int(s)}"
    if s.startswith("S_T"):
        tail = s[3:]
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            return f"T{int(digits)}"
    return None

def infer_line_code_from_text(*texts: str) -> Optional[str]:
    for t in texts:
        if not t:
            continue
        s = t.upper()
        if "S_M1" in s or " METRO" in s or s.endswith("M1") or s in ("M1","M"):
            return "M"
        m = _line_pat.search(s)
        if m:
            return f"T{m.group(2)}"
        if s.startswith("S_T") and len(s) > 3 and s[3].isdigit():
            return f"T{int(s[3])}"
    return None

def route_short_from_ids(route_id: str, route_short: str, trip_id: Optional[str] = None) -> Optional[str]:
    cand = (route_short or "").strip()
    if route_id and route_id in ROUTE_SHORT_OVERRIDES:
        cand = ROUTE_SHORT_OVERRIDES[route_id]
    if not cand and route_id:
        cand = route_id
    short = normalize_line_code(cand)
    if not short:
        short = infer_line_code_from_text(route_id, trip_id)
    return short

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
        # Ignore services on disallowed routes early
        if not r or route_id not in gtfs.allowed_route_ids:
            continue
        short_raw = r.short if r else ""
        short = route_short_from_ids(route_id, short_raw, trip_id=trip_id)
        stops = upcoming.get(trip_id, [])
        if not stops or not short:
            continue
        # Keep only stops whose parent is allowed — trim noise even earlier
        filtered = [sid for sid in stops if gtfs.parent_of(sid) in gtfs.allowed_parent_ids]
        if not filtered:
            continue
        trains.append(TrainObs(trip_id=trip_id, route_id=route_id, route_short=short, upcoming_stop_ids=filtered))
    return trains

# -----------------------------
# Background loop
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
                log.info("[debug] upcoming sizes: trains=%d metro=%d", len(up_trains), len(up_metro))

            trains_obs = build_train_obs(state.gtfs, pairs_trains, up_trains)
            metro_obs  = build_train_obs(state.gtfs, pairs_metro,  up_metro)

            state.set_trains(trains_obs + metro_obs)

            if MB_VERBOSE:
                for t in state.trains[:10]:
                    log.info("[debug] trip=%s route_id=%s short=%s first_stops=%s",
                             t.trip_id, t.route_id, t.route_short, t.upcoming_stop_ids[:5])

            log.info("[metroboard] processing trains=%d allowed_parents=%d static_available=%s",
                     len(state.trains), len(state.gtfs.allowed_parent_ids), state.gtfs.available)

        except Exception as e:
            log.exception("refresh_loop error: %s", e)

        await asyncio.sleep(POLL_SECONDS)

# -----------------------------
# FastAPI
# -----------------------------
gtfs = StaticGTFS(GTFS_DIR)
state = LiveState(gtfs)
app = FastAPI(title="Metroboard (rail+metro strict)")

@app.on_event("startup")
async def _startup():
    asyncio.create_task(refresh_loop(state))
    log.info("Application startup complete. strict_line_match=%s", STRICT_LINE_MATCH)

@app.get("/healthz")
async def health():
    return {
        "ok": True,
        "trains_cached": len(state.trains),
        "static_gtfs_available": state.gtfs.available,
        "allowed_routes": len(state.gtfs.allowed_route_ids),
        "allowed_parents": len(state.gtfs.allowed_parent_ids),
        "gtfs_dir": GTFS_DIR,
        "route_types_allow": sorted(ROUTE_TYPES_ALLOW),
        "agency_allow": sorted(AGENCY_ALLOW),
    }

@app.get("/stations")
async def stations(q: Optional[str] = Query(default=None)):
    """Debug: list allowed parent stations (optionally filter by substring)."""
    items = []
    for pid in sorted(state.gtfs.allowed_parent_ids):
        name = state.gtfs.parent_to_name.get(pid, "")
        if q and q.lower() not in name.lower():
            continue
        items.append({"parent_id": pid, "name": name})
    return items

@app.get("/states")
async def get_states(only: Optional[str] = Query(default=None)):
    if not only:
        return JSONResponse({})
    tokens = [t for t in only.split(",") if t.strip()]
    counts = state.counts_for_tokens(tokens)
    return JSONResponse(counts)

# run:
# uvicorn app:app --host 0.0.0.0 --port 8000
