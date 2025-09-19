# app.py — Metroboard backend (Rail + Metro only, production)
# - Requires static GTFS files: stops.txt, routes.txt, trips.txt, stop_times.txt, agency.txt
# - Filters to route_type in (1=subway/metro, 2=rail); agencies filter optional (empty=set() disables)
# - Builds station×route entries so "Central1" (T1) and "CentralM" (M1) are distinct keys
# - Station whitelist supported (names or numeric IDs), optional bbox guardrail
# - GTFS-RT source: Sydney Trains vehicle positions (Metro feed 404s on TfNSW)
# - States logic prefers VehiclePosition.stop_id+current_status, falls back to distance
# - Endpoints: /, /healthz, /debug, /stations, /states

import os, time, csv, random, typing as T, re, json
import requests
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.transit import gtfs_realtime_pb2
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- Config ----------------
API_TOKEN = os.environ["TFNSW_API_TOKEN"]

URLS_VP = [
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
    # "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneymetro",  # (currently 404)
]

# Distance thresholds (meters)
D_SOLID = int(os.getenv("MB_D_SOLID", "120"))
D_NEAR  = int(os.getenv("MB_D_NEAR",  "500"))

# Agencies to include (empty=set() means no filter)
# You can set MB_AGENCIES="Sydney Trains,Sydney Metro" in env to enable a filter.
AGENCY_NAME_ALLOW: set[str] = set(a.strip() for a in os.getenv("MB_AGENCIES","").split(",") if a.strip())

ROUTE_SHORT_OVERRIDES_JSON = os.getenv("ROUTE_SHORT_OVERRIDES_JSON", "")
ROUTE_SHORT_OVERRIDES_PATH = os.getenv("ROUTE_SHORT_OVERRIDES_PATH", "route_overrides.csv")

# Route short-names allowlist (empty=set() means no filter)
# Example: MB_ROUTE_SHORT_ALLOW="T1,T2,M1"
ROUTE_SHORT_ALLOW: set[str] = set(s.strip().upper() for s in os.getenv("MB_ROUTE_SHORT_ALLOW","").split(",") if s.strip())

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

# Name excludes (belt-and-braces)
EXCLUDE_NAME_SUBSTR = tuple(x.lower() for x in [
    "wharf", "light rail", "b-line", "interchange", "motorway", "m2",
])

# Regex for deriving T*/M* from route_long_name
SHORT_RE = re.compile(r'\b([TM]\d{1,2})\b', re.IGNORECASE)

# ---------------- Small helpers ----------------
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
    a = max(0.0, min(1.0, a))
    return 2 * R * asin(sqrt(a))

def within_bbox(lat: float, lon: float, bbox: tuple[float,float,float,float]) -> bool:
    S, W, N, E = bbox
    return (S <= lat <= N) and (W <= lon <= E)

def is_numeric_parent_stop_id(s: str) -> bool:
    return s.isdigit()

def status_from_distance(m: T.Optional[float]) -> int:
    if m is None: return 0
    if m <= D_SOLID: return 3
    if m <= D_NEAR:  return 1
    return 0

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

def _is_stationish(name: str) -> bool:
    n = (name or "").strip().lower()
    if any(s in n for s in EXCLUDE_NAME_SUBSTR):
        return False
    return n.endswith(" station") or n.endswith(" railway station")

# ---------------- Route short names (with overrides) ----------------
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
            rs = overrides.get(rid)
            if not rs:
                rs = derive_route_short(row.get("route_short_name"), row.get("route_long_name"))
            if rs:
                out[rid] = rs
    print(f"[metroboard] route_short mapping: {len(out)} entries (incl. overrides={len(overrides)})")
    return out

# ---------------- Static GTFS filters: allowed parents & pairs ----------------
def require_allowed_parents(
    stops_path="stops.txt",
    routes_path="routes.txt",
    trips_path="trips.txt",
    stop_times_path="stop_times.txt",
    agency_path="agency.txt",
    route_types_allowed=(1,2),
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
            if route_short_allow and rshort and rshort.upper() not in route_short_allow:
                continue

            p = parent_of(stop_id)
            if p and is_numeric_parent_stop_id(p):
                allowed_parents.add(p)

    print(f"[metroboard] routes: route_types_seen={sorted(seen_types)} agencies_seen={sorted(seen_agencies)}")
    if allowed_parents:
        print(f"[metroboard] Mode filter (by trips): {len(allowed_parents)} parent stations")
        return allowed_parents

    # Fallback: name-based station filter
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

def _known_tm_shorts(route_id_to_short: dict[str, str]) -> set[str]:
    out: set[str] = set()
    for rs in route_id_to_short.values():
        rsu = (rs or "").upper().strip()
        if re.fullmatch(r"[TM]\d{1,2}", rsu):
            out.add(rsu)
    return out

def require_allowed_pairs(
    stops_path="stops.txt",
    routes_path="routes.txt",
    trips_path="trips.txt",
    stop_times_path="stop_times.txt",
    agency_path="agency.txt",
    route_types_allowed=(1, 2),
    agencies_allowed: T.Set[str] = AGENCY_NAME_ALLOW,
    route_short_allow: T.Set[str] = ROUTE_SHORT_ALLOW,
    allowed_parents_for_fallback: T.Optional[set[str]] = None,
) -> set[tuple[str, str]]:
    """Return set of (parent_station_id, route_short).
    Strict path: join stop_times→trips→routes, require T*/M*.
    Fallback: if zero, pair every allowed parent with every known T*/M* code.
    """

    # agency_id -> name
    agency_name_by_id: dict[str, str] = {}
    with open(agency_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            agency_name_by_id[(r.get("agency_id") or "").strip()] = (r.get("agency_name") or "").strip()

    # route_id -> (route_type, agency_name, long_name)
    route_type_agency_long: dict[str, tuple[int, str, str]] = {}
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
    trip_route: dict[str, str] = {}
    with open(trips_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            trip_route[(r.get("trip_id") or "").strip()] = (r.get("route_id") or "").strip()

    # stops + parents
    stops_map: dict[str, dict] = {}
    parent_ids: set[str] = set()
    with open(stops_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("stop_id") or "").strip()
            stops_map[sid] = r
            if (r.get("location_type") or "0").strip() == "1":
                parent_ids.add(sid)

    def parent_of(sid: str) -> T.Optional[str]:
        row = stops_map.get(sid)
        if not row:
            return None
        p = (row.get("parent_station") or "").strip()
        if p:
            return p
        return sid if sid in parent_ids else None

    route_id_to_short = load_route_shortnames(routes_path)

    # ---------- STRICT PATH ----------
    pairs: set[tuple[str, str]] = set()
    missing_short_seen: dict[str, int] = {}

    with open(stop_times_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            trip_id = (r.get("trip_id") or "").strip()
            stop_id = (r.get("stop_id") or "").strip()
            rid = trip_route.get(trip_id, "")
            rtype, agname, _ = route_type_agency_long.get(rid, (None, "", ""))

            if rtype is None:
                continue
            if route_types_allowed and rtype not in route_types_allowed:
                continue
            if agencies_allowed and agname not in agencies_allowed:
                continue

            rshort = (route_id_to_short.get(rid, "") or "").upper()
            if not rshort:
                if len(missing_short_seen) < 10:
                    missing_short_seen[rid] = missing_short_seen.get(rid, 0) + 1
                continue

            if route_short_allow and rshort not in route_short_allow:
                continue

            p = parent_of(stop_id)
            if p and is_numeric_parent_stop_id(p):
                pairs.add((p, rshort))

    if pairs:
        print(f"[metroboard] Pair filter: {len(pairs)} station×route pairs")
        return pairs

    # ---------- PERMISSIVE FALLBACK ----------
    if os.getenv("MB_PERMISSIVE_PAIRS", "1") != "0":
        known_tm = _known_tm_shorts(route_id_to_short)
        if route_short_allow:
            known_tm &= {x.upper() for x in route_short_allow}
        if not known_tm:
            print("[metroboard] WARN: permissive fallback has no T*/M* codes to use — keeping empty pairs.")
            return set()

        if not allowed_parents_for_fallback:
            allowed_parents_for_fallback = {
                sid for sid, row in stops_map.items()
                if (row.get("location_type") or "0").strip() == "1" and is_numeric_parent_stop_id(sid)
            }

        fanout_pairs = {(pid, rs) for pid in allowed_parents_for_fallback for rs in known_tm}
        print(
            f"[metroboard] WARN: strict pair build found 0; using permissive fallback with "
            f"{len(known_tm)} routes × {len(allowed_parents_for_fallback)} parents = {len(fanout_pairs)} pairs"
        )
        return fanout_pairs

    # ---------- HARD FAIL ----------
    if missing_short_seen:
        print("[metroboard] WARN: Zero station×route pairs; sample route_ids with no short code:")
        for rid, _ in list(missing_short_seen.items())[:10]:
            meta = route_type_agency_long.get(rid, (None, "", ""))
            print(f"  - rid={rid}  agency={meta[1]!r}  rtype={meta[0]}  (add to overrides)")
    raise RuntimeError(
        "[metroboard] No (station, route) pairs found. Your static GTFS lacks T*/M* short codes. "
        "Provide overrides, or set MB_PERMISSIVE_PAIRS=1 (default) to synthesize pairs."
    )


# ---------------- Build station index (per station × route) ----------------
def route_display_suffix(route_short: str) -> str:
    """T1->'1', T9->'9', M1->'M' (for keys like Central1 / CentralM)."""
    rs = (route_short or "").strip().upper()
    if rs.startswith("T") and len(rs) >= 2:
        return rs[1:]
    if rs.startswith("M"):
        return "M"
    return rs or "?"

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

    # Group allowed_pairs by parent station
    by_parent: dict[str, set[str]] = {}
    for pid, rshort in allowed_pairs:
        by_parent.setdefault(pid, set()).add(rshort)

    index: dict[str, dict] = {}
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

        want_base = True
        if sid_whitelist or name_whitelist:
            want_base = (pid in sid_whitelist) or (clean_station_name(base_name).lower() in name_whitelist)
        if want_base and bbox is not None and not within_bbox(lat, lon, bbox):
            if pid not in kids:
                want_base = False
        if not want_base:
            continue

        for rshort in sorted(by_parent.get(pid, set())):
            suffix = route_display_suffix(rshort)
            key = f"{base_name}{suffix}"
            if key in index:
                key = f"{base_name}{suffix} ({pid})"
            index[key] = {
                "stop_id": pid,       # parent id (numeric)
                "name": key,
                "lat": lat,
                "lon": lon,
                "route_short": rshort,  # e.g. "T9", "M1"
                "base_name": base_name,
            }

    return index

# ---------------- Child→Parent map for VP.stop_id resolution ----------------
def build_child_to_parent(stops_path="stops.txt") -> dict[str, str]:
    out: dict[str,str] = {}
    with open(stops_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = (r.get("stop_id") or "").strip()
            pid = (r.get("parent_station") or "").strip()
            if sid and pid and is_numeric_parent_stop_id(pid):
                out[sid] = pid
            loc_type = (r.get("location_type") or "0").strip()
            if sid and loc_type == "1" and is_numeric_parent_stop_id(sid):
                out.setdefault(sid, sid)
    print(f"[metroboard] child→parent map: {len(out)} entries")
    return out

# ---------------- GTFS-RT: fetch & parse ----------------
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

# Prefer VP.stop_id + current_status, fallback to distance
# Returns list[(lat, lon, rshort, stop_id, current_status)]
def collect_allowed_trains(
    feed: gtfs_realtime_pb2.FeedMessage,
    route_id_to_short: dict[str,str]
) -> list[tuple[T.Optional[float],T.Optional[float],str,str,int]]:
    VP_INCOMING_AT   = gtfs_realtime_pb2.VehiclePosition.INCOMING_AT
    VP_STOPPED_AT    = gtfs_realtime_pb2.VehiclePosition.STOPPED_AT
    VP_IN_TRANSIT_TO = gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO

    trains: list[tuple[T.Optional[float],T.Optional[float],str,str,int]] = []
    for e in feed.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        rid = v.trip.route_id if v.HasField("trip") else ""
        rshort = (route_id_to_short.get(rid, "") or "").upper()
        if not rshort:
            continue
        if ROUTE_SHORT_ALLOW and rshort not in ROUTE_SHORT_ALLOW:
            continue

        sid = getattr(v, "stop_id", "")
        status = getattr(v, "current_status", VP_IN_TRANSIT_TO)
        lat = getattr(v.position, "latitude", None) if v.HasField("position") else None
        lon = getattr(v.position, "longitude", None) if v.HasField("position") else None

        trains.append((lat, lon, rshort, sid, int(status)))
    vlog(f"[metroboard] collect_allowed_trains: trains={len(trains)}")
    return trains

# ---------------- FastAPI app ----------------
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
CHILD_TO_PARENT: dict[str, str] = {}

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
        route_types_allowed=(1, 2),  # Metro + Rail only
        agencies_allowed=AGENCY_NAME_ALLOW,
        route_short_allow=ROUTE_SHORT_ALLOW,
    )

    allowed_pairs = require_allowed_pairs(
        stops_path="stops.txt",
        routes_path="routes.txt",
        trips_path="trips.txt",
        stop_times_path="stop_times.txt",
        agency_path="agency.txt",
        route_types_allowed=(1, 2),
        agencies_allowed=AGENCY_NAME_ALLOW,
        route_short_allow=ROUTE_SHORT_ALLOW,
        allowed_parents_for_fallback=allowed_parents,  # ← important for fallback fanout
    )

    wl_path = os.getenv("STATION_WHITELIST_PATH", "stations_whitelist.txt")
    sid_w, name_w = load_whitelist(wl_path)

    STATION_INDEX = load_station_index(
        "stops.txt",
        bbox=SYD_BBOX,
        sid_whitelist=sid_w,
        name_whitelist=name_w,
        allowed_parents=allowed_parents,
        allowed_pairs=allowed_pairs,
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

    # No trains -> all zeros
    out: dict[str, int] = {}
    if not trains:
        for name in subset.keys():
            out[name] = 0
        return JSONResponse(out)

    # Group trains by route
    trains_by_route: dict[str, list[tuple[T.Optional[float],T.Optional[float],str,str,int]]] = {}
    for rec in trains:
        _, _, rshort, _, _ = rec
        trains_by_route.setdefault(rshort, []).append(rec)

    # VP current_status enum aliases
    VP_INCOMING_AT   = gtfs_realtime_pb2.VehiclePosition.INCOMING_AT
    VP_STOPPED_AT    = gtfs_realtime_pb2.VehiclePosition.STOPPED_AT
    # VP_IN_TRANSIT_TO = gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO  # not used directly

    for name, meta in subset.items():
        station_pid = meta["stop_id"]               # numeric parent id
        slat, slon = meta["lat"], meta["lon"]
        rshort = (meta.get("route_short") or "").upper()

        best_state_vp = 0  # 0 none, 1 near, 3 solid
        best_dist = None   # float meters for fallback

        for (tlat, tlon, trs, child_sid, status) in trains_by_route.get(rshort, []):
            # --- Prefer stop_id + status ---
            parent = None
            if child_sid:
                parent = CHILD_TO_PARENT.get(child_sid)
                if parent is None and is_numeric_parent_stop_id(child_sid):
                    parent = child_sid
            if parent == station_pid:
                if status == VP_STOPPED_AT:
                    best_state_vp = 3
                    break  # cannot do better
                elif status == VP_INCOMING_AT:
                    best_state_vp = max(best_state_vp, 1)
                # IN_TRANSIT_TO => distance fallback continues

            # --- Distance fallback ---
            if best_state_vp < 3 and tlat is not None and tlon is not None:
                d = haversine_m(slat, slon, tlat, tlon)
                best_dist = d if best_dist is None else min(best_dist, d)

        out[name] = best_state_vp if best_state_vp > 0 else status_from_distance(best_dist)

    return JSONResponse(out)
