# app.py — Metroboard backend (Rail + Metro only, production)
# - Requires static GTFS files: stops.txt, routes.txt, trips.txt, stop_times.txt, agency.txt
# - Filters to route_type in (1=subway/metro, 2=rail) and agencies {"Sydney Trains","NSW TrainLink","Sydney Metro"}
# - Builds station×route entries so "Central1" (T1) and "CentralM" (M1) are distinct keys
# - Optional station whitelist (names or numeric IDs)
# - GTFS-RT source: Sydney Trains vehicle positions (Metro feed 404s on TfNSW)
# - Endpoints: /, /healthz, /debug, /stations, /states

import os, time, math, csv, random, typing as T
import requests
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.transit import gtfs_realtime_pb2
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import re
import json

# ---------------- Config ----------------
API_TOKEN = os.environ["TFNSW_API_TOKEN"]

URLS_VP = [
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
    # Metro feed currently 404s on TfNSW; leave disabled:
    # "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneymetro",
]

# Distance thresholds (meters)
D_SOLID = 120
D_NEAR  = 500

# Agencies to include (empty=set() means no filter)
AGENCY_NAME_ALLOW: set[str] = set()  # <- change to empty set
# (remove "NSW TrainLink" for now — it usually has no T*/M* code)

ROUTE_SHORT_OVERRIDES_JSON = os.getenv("ROUTE_SHORT_OVERRIDES_JSON", "")
ROUTE_SHORT_OVERRIDES_PATH = os.getenv("ROUTE_SHORT_OVERRIDES_PATH", "route_overrides.csv")
SHORT_RE = re.compile(r'\b([TM]\d{1,2})\b', re.IGNORECASE)

# Route short-names allowlist (empty=set() means no filter)
ROUTE_SHORT_ALLOW: set[str] = set()

# Caching + CORS
FEED_CACHE_SEC = int(os.getenv("FEED_CACHE_SEC", "3"))
CORS_ALLOW_ORIGIN = os.getenv("CORS_ALLOW_ORIGIN")

# Optional geographic guardrail
DEFAULT_SYD_BBOX = (-34.2, 150.5, -33.4, 151.4)
def parse_bbox_env() -> T.Optional[tuple[float,float,float,float]]:
    if os.getenv("SYD_BBOX_DISABLE") == "1":
        return None
    s = os.getenv("SYD_BBOX")
    if not s:
        return DEFAULT_SYD_BBOX
    try:
        a,b,c,d = [float(x.strip()) for x in s.split(",")]
        return (a,b,c,d)
    except Exception:
        return DEFAULT_SYD_BBOX
SYD_BBOX = parse_bbox_env()

# Log verbosity (set MB_VERBOSE=1 to enable extra logs)
MB_VERBOSE = os.getenv("MB_VERBOSE", "0") == "1"
def vlog(msg: str):
    if MB_VERBOSE:
        print(msg)

# Belt-and-braces name excludes
EXCLUDE_NAME_SUBSTR = tuple(x.lower() for x in [
    "wharf", "light rail", "b-line", "interchange", "motorway", "m2",
])

# ---------------- Helpers ----------------
SHORT_RE = re.compile(r'\b([TM]\d{1,2})\b', re.IGNORECASE)

def derive_route_short(route_short_name: str, route_long_name: str) -> str:
    rs = (route_short_name or "").strip()
    if rs:
        return rs.upper()
    rl = (route_long_name or "").strip()
    if rl:
        m = SHORT_RE.search(rl)
        if m:
            return m.group(1).upper()
    return ""

def load_route_overrides(path: str, env_json: str) -> dict[str, str]:
    """
    Load a user-provided mapping route_id -> route_short_name from:
      - CSV file (route_id, route_short_name)
      - ENV JSON string {"<route_id>":"T1", ...}
    Later sources override earlier ones.
    """
    out: dict[str, str] = {}
    # file (CSV)
    try:
        if path and os.path.exists(path):
            with open(path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    rid = (row.get("route_id") or "").strip()
                    rs  = (row.get("route_short_name") or "").strip().upper()
                    if rid and rs:
                        out[rid] = rs
            print(f"[metroboard] route_overrides: loaded {len(out)} from {path}")
    except Exception as e:
        print(f"[metroboard] WARN: failed reading {path}: {e}")

    # env JSON
    if env_json:
        try:
            obj = json.loads(env_json)
            if isinstance(obj, dict):
                for rid, rs in obj.items():
                    rid = (rid or "").strip()
                    rs  = (rs or "").strip().upper()
                    if rid and rs:
                        out[rid] = rs
                print(f"[metroboard] route_overrides: merged {len(obj)} from env JSON")
        except Exception as e:
            print(f"[metroboard] WARN: bad ROUTE_SHORT_OVERRIDES_JSON: {e}")

    return out


def is_numeric_parent_stop_id(s: str) -> bool:
    return s.isdigit()

def status_from_distance(m: T.Optional[float]) -> int:
    if m is None: return 0
    if m <= D_SOLID: return 3
    if m <= D_NEAR:  return 1
    return 0

def session():
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=0.6,
        status_forcelist=(500,502,503,504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

def _valid_ll(lat: float, lon: float) -> bool:
    try:
        lat = float(lat); lon = float(lon)
    except Exception:
        return False
    return (-90.0 <= lat <= 90.0) and (-180.0 <= lon <= 180.0)

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters. Guards against bad inputs and unit mistakes."""
    try:
        lat1 = float(lat1); lon1 = float(lon1); lat2 = float(lat2); lon2 = float(lon2)
    except Exception:
        return float("inf")
    if not (_valid_ll(lat1, lon1) and _valid_ll(lat2, lon2)):
        return float("inf")

    from math import radians, sin, cos, asin, sqrt
    R = 6371000.0  # meters
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2) ** 2
    if a < 0.0: a = 0.0
    if a > 1.0: a = 1.0
    return 2 * R * asin(sqrt(a))

def load_route_shortnames(path="routes.txt") -> dict[str,str]:
    """
    Build route_id -> route_short_name.
    Priority:
      1) user overrides (CSV / env JSON)
      2) GTFS route_short_name
      3) token 'T*/M*' derived from route_long_name
    """
    overrides = load_route_overrides(ROUTE_SHORT_OVERRIDES_PATH, ROUTE_SHORT_OVERRIDES_JSON)

    out: dict[str,str] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rid = (row.get("route_id") or "").strip()
            if not rid:
                continue
            # start with override if present
            rs = overrides.get(rid)
            if not rs:
                rs = derive_route_short(row.get("route_short_name"), row.get("route_long_name"))
            if rs:
                out[rid] = rs
    print(f"[metroboard] route_short mapping: {len(out)} entries "
          f"(incl. overrides={len(load_route_overrides(ROUTE_SHORT_OVERRIDES_PATH, ROUTE_SHORT_OVERRIDES_JSON))})")
    return out



def within_bbox(lat: float, lon: float, bbox: tuple[float,float,float,float]) -> bool:
    S, W, N, E = bbox
    return (S <= lat <= N) and (W <= lon <= E)

def clean_station_name(name: str) -> str:
    n = (name or "").strip()
    for suf in (" Station", " Railway Station"):
        if n.endswith(suf):
            n = n[: -len(suf)]
    return n

def load_whitelist(path: str) -> tuple[set[str], set[str]]:
    sid_w: set[str] = set()
    name_w: set[str] = set()
    if not path or not os.path.exists(path):
        return sid_w, name_w
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            token = line.split(",", 1)[0].strip()
            if token.isdigit():
                sid_w.add(token)
            else:
                name_w.add(clean_station_name(token).lower())
    return sid_w, name_w

# ---------------- Static GTFS filter: Rail + Metro only ----------------
def _is_stationish(name: str) -> bool:
    n = (name or "").strip().lower()
    if any(s in n for s in EXCLUDE_NAME_SUBSTR):
        return False
    return n.endswith(" station") or n.endswith(" railway station")

def require_allowed_parents(
    stops_path="stops.txt",
    routes_path="routes.txt",
    trips_path="trips.txt",
    stop_times_path="stop_times.txt",
    agency_path="agency.txt",
    route_types_allowed=(1,2),   # Metro (1) + Rail (2)
    agencies_allowed: T.Set[str] = AGENCY_NAME_ALLOW,
    route_short_allow: T.Set[str] = ROUTE_SHORT_ALLOW,
) -> set[str]:
    # Ensure files exist
    missing = [p for p in (stops_path, routes_path, trips_path, stop_times_path, agency_path) if not os.path.exists(p)]
    if missing:
        raise RuntimeError("[metroboard] Missing static GTFS files: " + ", ".join(missing))

    # agency_id -> name
    agency_name_by_id: dict[str,str] = {}
    with open(agency_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            agency_name_by_id[(r.get("agency_id") or "").strip()] = (r.get("agency_name") or "").strip()

    # route_id -> (route_type, short, agency_name)
    route_meta: dict[str, tuple[int,str,str]] = {}
    with open(routes_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid = (r.get("route_id") or "").strip()
            try:
                rtype = int((r.get("route_type") or "3").strip() or "3")
            except Exception:
                rtype = 3
            rshort = (r.get("route_short_name") or "").strip()
            agid = (r.get("agency_id") or "").strip()
            agname = agency_name_by_id.get(agid, "")
            route_meta[rid] = (rtype, rshort, agname)

    # trip_id -> route_id
    trip_route: dict[str,str] = {}
    with open(trips_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            trip_route[(r.get("trip_id") or "").strip()] = (r.get("route_id") or "").strip()

    # stops + parents
    stops_map: dict[str,dict] = {}
    parent_ids: set[str] = set()
    with open(stops_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("stop_id") or "").strip()
            stops_map[sid] = r
            if (r.get("location_type") or "0").strip() == "1":
                parent_ids.add(sid)

    def parent_of(sid: str) -> T.Optional[str]:
        row = stops_map.get(sid)
        if not row: return None
        p = (row.get("parent_station") or "").strip()
        if p: return p
        return sid if sid in parent_ids else None

    # ---------- STRICT PATH: by trips/stop_times ----------
    allowed_parents: set[str] = set()
    seen_types, seen_agencies = set(), set()
    with open(stop_times_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            trip_id = (r.get("trip_id") or "").strip()
            stop_id = (r.get("stop_id") or "").strip()
            rid = trip_route.get(trip_id, "")
            rtype, rshort, agname = route_meta.get(rid, (None, "", ""))

            if rtype is None:
                continue
            seen_types.add(rtype)
            if agname: seen_agencies.add(agname)

            if route_types_allowed and rtype not in route_types_allowed:
                continue
            if agencies_allowed and agname not in agencies_allowed:
                continue
            if route_short_allow and rshort and rshort not in route_short_allow:
                continue

            p = parent_of(stop_id)
            if p and is_numeric_parent_stop_id(p):
                allowed_parents.add(p)

    print(f"[metroboard] routes: route_types_seen={sorted(seen_types)} agencies_seen={sorted(seen_agencies)}")
    if allowed_parents:
        print(f"[metroboard] Mode filter (by trips): {len(allowed_parents)} parent stations")
        return allowed_parents

    # ---------- FALLBACK PATH: by station-like parent names ----------
    print("[metroboard] WARN: trip-based filter yielded 0 parents; falling back to name-based station filter.")
    fallback_parents: set[str] = set()
    with open(stops_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("location_type") or "0").strip() != "1":
                continue
            pid = (r.get("stop_id") or "").strip()
            if not is_numeric_parent_stop_id(pid):
                continue
            raw_name = (r.get("stop_name") or "").strip()
            if not _is_stationish(raw_name):
                continue
            fallback_parents.add(pid)

    if not fallback_parents:
        raise RuntimeError("[metroboard] Name-based fallback produced zero parents — check GTFS stops.txt")

    print(f"[metroboard] Mode filter (by names): {len(fallback_parents)} parent stations")
    return fallback_parents

# ---------------- Static GTFS: station × route_short_name pairs ----------------
def require_allowed_pairs(
    stops_path="stops.txt",
    routes_path="routes.txt",
    trips_path="trips.txt",
    stop_times_path="stop_times.txt",
    agency_path="agency.txt",
    route_types_allowed=(1,2),   # Metro (1) + Rail (2)
    agencies_allowed: T.Set[str] = AGENCY_NAME_ALLOW,
    route_short_allow: T.Set[str] = ROUTE_SHORT_ALLOW,
) -> set[tuple[str, str]]:
    # agency_id -> name
    agency_name_by_id: dict[str,str] = {}
    with open(agency_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            agency_name_by_id[(r.get("agency_id") or "").strip()] = (r.get("agency_name") or "").strip()

    # route_id -> (route_type, agency_name, long_name)
    route_type_agency_long: dict[str, tuple[int,str,str]] = {}
    with open(routes_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid = (r.get("route_id") or "").strip()
            try:
                rtype = int((r.get("route_type") or "3").strip() or "3")
            except Exception:
                rtype = 3
            agid = (r.get("agency_id") or "").strip()
            agname = agency_name_by_id.get(agid, "")
            rlong = (r.get("route_long_name") or "").strip()
            if rid:
                route_type_agency_long[rid] = (rtype, agname, rlong)

    # trip_id -> route_id
    trip_route: dict[str,str] = {}
    with open(trips_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            trip_route[(r.get("trip_id") or "").strip()] = (r.get("route_id") or "").strip()

    # stops + parents
    stops_map: dict[str,dict] = {}
    parent_ids: set[str] = set()
    with open(stops_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("stop_id") or "").strip()
            stops_map[sid] = r
            if (r.get("location_type") or "0").strip() == "1":
                parent_ids.add(sid)

    def parent_of(sid: str) -> T.Optional[str]:
        row = stops_map.get(sid)
        if not row: return None
        p = (row.get("parent_station") or "").strip()
        if p: return p
        return sid if sid in parent_ids else None

    # route_id -> short (with overrides applied already)
    route_id_to_short = load_route_shortnames(routes_path)

    pairs: set[tuple[str,str]] = set()
    missing_short_seen: dict[str, int] = {}

    with open(stop_times_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            trip_id = (r.get("trip_id") or "").strip()
            stop_id = (r.get("stop_id") or "").strip()
            rid = trip_route.get(trip_id, "")
            rtype, agname, _rlong = route_type_agency_long.get(rid, (None, "", ""))

            if rtype is None:
                continue

            # Filters
            if route_types_allowed and rtype not in route_types_allowed:
                continue
            if agencies_allowed and agname not in agencies_allowed:
                continue

            rshort = route_id_to_short.get(rid, "")
            if not rshort:
                # collect a limited sample for helpful logs
                if len(missing_short_seen) < 10:
                    missing_short_seen[rid] = missing_short_seen.get(rid, 0) + 1
                continue
            if route_short_allow and rshort not in route_short_allow:
                continue

            p = parent_of(stop_id)
            if p and is_numeric_parent_stop_id(p):
                pairs.add((p, rshort))

    if not pairs:
        if missing_short_seen:
            print("[metroboard] WARN: Zero station×route pairs; sample route_ids with no short code:")
            for rid, _ in list(missing_short_seen.items())[:10]:
                meta = route_type_agency_long.get(rid, (None,"",""))
                print(f"  - rid={rid}  agency={meta[1]!r}  rtype={meta[0]}  (add to overrides)")
        raise RuntimeError(
            "[metroboard] No (station, route) pairs found. Your static GTFS lacks T*/M* short codes. "
            "Provide an override mapping via route_overrides.csv or ROUTE_SHORT_OVERRIDES_JSON."
        )

    print(f"[metroboard] Pair filter: {len(pairs)} station×route pairs")
    return pairs



# ---------------- Build station index (per station × route) ----------------
def route_display_suffix(route_short: str) -> str:
    """T1->'1', T9->'9', M1->'M' (per Central1 / CentralM examples)."""
    rs = (route_short or "").strip().upper()
    if rs.startswith("T") and len(rs) >= 2:
        return rs[1:]  # digits only
    if rs.startswith("M"):
        return "M"
    return rs or "?"

def same_line(a: str, b: str) -> bool:
    return (a or "").upper() == (b or "").upper()

def load_station_index(
    stops_path="stops.txt",
    bbox: T.Optional[tuple[float,float,float,float]] = None,
    sid_whitelist: set[str] = set(),
    name_whitelist: set[str] = set(),
    allowed_parents: T.Optional[set[str]] = None,
    allowed_pairs: T.Optional[set[tuple[str,str]]] = None,
):
    if allowed_parents is None:
        raise RuntimeError("[metroboard] allowed_parents required")
    if allowed_pairs is None:
        raise RuntimeError("[metroboard] allowed_pairs required")

    parents: dict[str, dict] = {}
    kids: dict[str, list[tuple[float,float]]] = {}

    with open(stops_path, newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        for row in r:
            loc_type = int((row.get("location_type") or "0").strip() or "0")
            stop_id  = (row.get("stop_id") or "").strip()
            parent   = (row.get("parent_station") or "").strip()
            name_raw = (row.get("stop_name") or "").strip()
            lat_s, lon_s = row.get("stop_lat"), row.get("stop_lon")
            lat = float(lat_s) if lat_s else None
            lon = float(lon_s) if lon_s else None

            if loc_type == 1:
                if not is_numeric_parent_stop_id(stop_id):
                    continue
                if stop_id not in allowed_parents:
                    continue
                if any(s in name_raw.lower() for s in EXCLUDE_NAME_SUBSTR):
                    continue
                parents[stop_id] = {"stop_id": stop_id, "raw_name": name_raw, "lat": lat, "lon": lon}
            else:
                if parent and lat is not None and lon is not None:
                    if (bbox is None) or within_bbox(lat, lon, bbox):
                        kids.setdefault(parent, []).append((lat, lon))

    # Build per-(station, route) entries
    index: dict[str, dict] = {}
    # Group allowed_pairs by parent station
    by_parent: dict[str, set[str]] = {}
    for pid, rshort in allowed_pairs:
        by_parent.setdefault(pid, set()).add(rshort)

    for pid, meta in parents.items():
        raw_name = meta["raw_name"]
        base_name = clean_station_name(raw_name)
        if any(s in base_name.lower() for s in EXCLUDE_NAME_SUBSTR):
            continue

        lat, lon = meta["lat"], meta["lon"]
        if (lat is None or lon is None) and pid in kids and kids[pid]:
            plat = kids[pid]
            lat = sum(p[0] for p in plat) / len(plat)
            lon = sum(p[1] for p in plat) / len(plat)
        if lat is None or lon is None:
            continue

        # Whitelist / bbox checks on the base station (apply to all routes under it)
        want_base = True
        if sid_whitelist or name_whitelist:
            want_base = (pid in sid_whitelist) or (clean_station_name(base_name).lower() in name_whitelist)
        if want_base and bbox is not None and not within_bbox(lat, lon, bbox):
            if pid not in kids:
                want_base = False
        if not want_base:
            continue

        # Fan-out per route_short_name
        rshorts = by_parent.get(pid, set())
        for rshort in sorted(rshorts):
            suffix = route_display_suffix(rshort)
            key = f"{base_name}{suffix}"
            # Ensure uniqueness if duplicated names appear
            if key in index:
                key = f"{base_name}{suffix} ({pid})"
            index[key] = {
                "stop_id": pid,
                "name": key,
                "lat": lat,
                "lon": lon,
                "route_short": rshort,        # keep the true route short ("T9", "M1")
                "base_name": base_name,       # optional: handy for UI
            }

    return index

# ---------------- Train feed (cached + merged) ----------------
_FEED_CACHE: T.Optional[gtfs_realtime_pb2.FeedMessage] = None
_FEED_TS = 0.0

def fetch_vehicle_positions_cached():
    global _FEED_CACHE, _FEED_TS
    now = time.time()
    if _FEED_CACHE is not None and (now - _FEED_TS) < FEED_CACHE_SEC:
        return _FEED_CACHE, (now - _FEED_TS)

    headers = {
        "Authorization": f"apikey {API_TOKEN}",
        "Accept": "application/x-protobuf",
        "Cache-Control": "no-cache",
        "User-Agent": "metroboard/1.0",
    }
    s = session()
    time.sleep(random.uniform(0, 0.25))

    merged = gtfs_realtime_pb2.FeedMessage()
    merged.header.gtfs_realtime_version = "2.0"

    for url in URLS_VP:
        try:
            r = s.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(r.content)
            merged.entity.extend(feed.entity)
            vlog(f"[metroboard] fetched {url}: entities={len(feed.entity)}")
        except Exception as e:
            print(f"[metroboard] WARN: failed fetching {url}: {e}")

    _FEED_CACHE = merged
    _FEED_TS = now
    return merged, 0.0

def collect_allowed_trains(feed: gtfs_realtime_pb2.FeedMessage,
                           route_id_to_short: dict[str,str]) -> list[tuple[float,float,str]]:
    trains: list[tuple[float,float,str]] = []
    for e in feed.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        rid = v.trip.route_id if v.HasField("trip") else ""
        rshort = route_id_to_short.get(rid, "")
        if ROUTE_SHORT_ALLOW and rshort and rshort not in ROUTE_SHORT_ALLOW:
            continue
        if not v.HasField("position"):
            continue
        lat = getattr(v.position, "latitude", None)
        lon = getattr(v.position, "longitude", None)
        if lat is None or lon is None or not rshort:
            continue
        trains.append((lat, lon, rshort))
    vlog(f"[metroboard] collect_allowed_trains: trains={len(trains)}")
    return trains

# ---------------- FastAPI ----------------
app = FastAPI()
if CORS_ALLOW_ORIGIN:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in CORS_ALLOW_ORIGIN.split(",")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

ROUTE_ID_TO_SHORT: dict[str,str] = {}
STATION_INDEX: dict[str, dict] = {}

# Build a quick lookup parent_id -> set({ 'T1','M1', ... })
def build_parent_to_shorts(pairs: set[tuple[str,str]]) -> dict[str, set[str]]:
    out: dict[str,set[str]] = {}
    for pid, rs in pairs:
        out.setdefault(pid, set()).add(rs)
    return out


@app.on_event("startup")
def _startup():
    global ROUTE_ID_TO_SHORT, STATION_INDEX
    ROUTE_ID_TO_SHORT = load_route_shortnames("routes.txt")

    allowed_parents = require_allowed_parents(
        stops_path="stops.txt",
        routes_path="routes.txt",
        trips_path="trips.txt",
        stop_times_path="stop_times.txt",
        agency_path="agency.txt",
        route_types_allowed=(1,2),                 # Metro + Rail only
        agencies_allowed=AGENCY_NAME_ALLOW,
        route_short_allow=ROUTE_SHORT_ALLOW
    )

    # NEW: compute per-(station, route) pairs
    allowed_pairs = require_allowed_pairs(
        stops_path="stops.txt",
        routes_path="routes.txt",
        trips_path="trips.txt",
        stop_times_path="stop_times.txt",
        agency_path="agency.txt",
        route_types_allowed=(1,2),
        agencies_allowed=AGENCY_NAME_ALLOW,
        route_short_allow=ROUTE_SHORT_ALLOW,
    )

    wl_path = os.getenv("STATION_WHITELIST_PATH", "stations_whitelist.txt")
    sid_w, name_w = load_whitelist(wl_path)

    STATION_INDEX = load_station_index(
        "stops.txt",
        bbox=SYD_BBOX,
        sid_whitelist=sid_w,
        name_whitelist=name_w,
        allowed_parents=allowed_parents,
        allowed_pairs=allowed_pairs,   # pass the new pairs in
    )

    if not STATION_INDEX:
        raise RuntimeError("[metroboard] No stations loaded after filtering — check whitelist and GTFS files.")

    print(f"[metroboard] Loaded {len(STATION_INDEX)} station×line entries (rail+metro only)")

@app.get("/")
def root():
    return {"ok": True, "endpoints": ["/states", "/stations", "/healthz", "/debug"]}

@app.get("/healthz")
def healthz():
    age = None if _FEED_TS == 0 else (time.time() - _FEED_TS)
    return {"status": "ok", "stations": len(STATION_INDEX), "trains_last_fetch_s": age}

@app.get("/debug")
def debug():
    return {
        "stations_count": len(STATION_INDEX),
        "bbox": list(SYD_BBOX) if SYD_BBOX else None,
        "allowed_routes": sorted(ROUTE_SHORT_ALLOW),
        "allowed_agencies": sorted(AGENCY_NAME_ALLOW),
        "cache_sec": FEED_CACHE_SEC,
        "feeds": URLS_VP,
        "verbose_logs": MB_VERBOSE,
    }

@app.get("/stations")
def stations():
    # Sorted by name for stable UI
    return JSONResponse([STATION_INDEX[k] for k in sorted(STATION_INDEX.keys())])

@app.get("/states")
def states(only: T.Optional[str] = Query(None, description="Comma-separated station names to restrict the output")):
    subset = STATION_INDEX
    if only:
        wanted = [n.strip() for n in only.split(",") if n.strip()]
        subset = {n: STATION_INDEX[n] for n in wanted if n in STATION_INDEX}
        if not subset:
            return JSONResponse({})

    feed, _ = fetch_vehicle_positions_cached()
    trains = collect_allowed_trains(feed, ROUTE_ID_TO_SHORT)

    out: dict[str, int] = {}
    if not trains:
        for name in subset.keys():
            out[name] = 0
        return JSONResponse(out)

    # Index trains by route for quick filtering
    trains_by_route: dict[str, list[tuple[float,float]]] = {}
    for tlat, tlon, rshort in trains:
        trains_by_route.setdefault(rshort.upper(), []).append((tlat, tlon))

    for name, meta in subset.items():
        slat, slon = meta["lat"], meta["lon"]
        rshort = (meta.get("route_short") or "").upper()
        cand = trains_by_route.get(rshort, [])
        best = None
        for (tlat, tlon) in cand:
            d = haversine_m(slat, slon, tlat, tlon)
            best = d if best is None else min(best, d)
        out[name] = status_from_distance(best)

    return JSONResponse(out)
