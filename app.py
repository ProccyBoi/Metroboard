# app.py — Metroboard backend (Rail + Metro only, strict)
# - Requires static GTFS files: stops.txt, routes.txt, trips.txt, stop_times.txt, agency.txt
# - Computes allowed parent stations by route_type in (1=subway/metro, 2=rail),
#   optionally filtered to agencies {"Sydney Trains","NSW Trains","Sydney Metro"}
# - NO fallback: if the mode filter can't be built, startup fails (to avoid wharfs/light rail/bus)
# - Supports a station whitelist (names or numeric IDs) layered on top
# - Merges GTFS-RT for Sydney Trains + Sydney Metro
# - Exposes: /, /healthz, /debug, /stations, /states

import os, time, math, csv, random, typing as T
import requests
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.transit import gtfs_realtime_pb2
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ------------- Config -------------
API_TOKEN = os.environ["TFNSW_API_TOKEN"]

URLS_VP = [
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
    # "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneymetro",  # 404 on TfNSW
]

# Distance thresholds (m)
D_SOLID = 120
D_NEAR  = 500

# Agencies to include (empty = no agency filtering)
AGENCY_NAME_ALLOW = {"Sydney Trains", "NSW TrainLink", "Sydney Metro"}

# Route short-names allowlist (empty = no short-name filtering)
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

# Belt-and-braces name excludes (extra guard)
EXCLUDE_NAME_SUBSTR = tuple(x.lower() for x in [
    "wharf", "light rail", "b-line", "interchange", "motorway", "m2",
])

# ------------- Helpers -------------
def is_numeric_parent_stop_id(s: str) -> bool:
    return s.isdigit()

def status_from_distance(m: T.Optional[float]) -> int:
    if m is None: return 0
    if m <= D_SOLID: return 3
    if m <= D_NEAR:  return 1
    return 0

def session():
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=0.6,
                  status_forcelist=(500,502,503,504),
                  allowed_methods=frozenset(["GET"]),
                  raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lat2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def load_route_shortnames(path="routes.txt") -> dict[str,str]:
    out: dict[str,str] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rid = (row.get("route_id") or "").strip()
            rs  = (row.get("route_short_name") or "").strip()
            if rid:
                out[rid] = rs
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

# ------------- Static GTFS filter: Rail + Metro only -------------
def _is_stationish(name: str) -> bool:
    n = (name or "").strip().lower()
    if any(s in n for s in EXCLUDE_NAME_SUBSTR):  # wharf, light rail, b-line, etc.
        return False
    return n.endswith(" station") or n.endswith(" railway station")

def require_allowed_parents(
    stops_path="stops.txt",
    routes_path="routes.txt",
    trips_path="trips.txt",
    stop_times_path="stop_times.txt",
    agency_path="agency.txt",
    route_types_allowed=(2,401),   # include common TfNSW encodings
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
            # keep it — coordinates checked later when building STATION_INDEX
            fallback_parents.add(pid)

    if not fallback_parents:
        raise RuntimeError("[metroboard] Name-based fallback produced zero parents — check GTFS stops.txt")

    print(f"[metroboard] Mode filter (by names): {len(fallback_parents)} parent stations")
    return fallback_parents

# ------------- Build station index -------------
def load_station_index(
    stops_path="stops.txt",
    bbox: T.Optional[tuple[float,float,float,float]] = None,
    sid_whitelist: set[str] = set(),
    name_whitelist: set[str] = set(),
    allowed_parents: T.Optional[set[str]] = None,
):
    if allowed_parents is None:
        raise RuntimeError("[metroboard] allowed_parents required")

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
                # extra name guard
                if any(s in name_raw.lower() for s in EXCLUDE_NAME_SUBSTR):
                    continue
                parents[stop_id] = {"stop_id": stop_id, "raw_name": name_raw, "lat": lat, "lon": lon}
            else:
                if parent and lat is not None and lon is not None:
                    if (bbox is None) or within_bbox(lat, lon, bbox):
                        kids.setdefault(parent, []).append((lat, lon))

    index: dict[str, dict] = {}
    for pid, meta in parents.items():
        raw_name = meta["raw_name"]
        name = clean_station_name(raw_name)
        # extra name guard (again on cleaned)
        if any(s in name.lower() for s in EXCLUDE_NAME_SUBSTR):
            continue

        lat, lon = meta["lat"], meta["lon"]
        if (lat is None or lon is None) and pid in kids and kids[pid]:
            plat = kids[pid]
            lat = sum(p[0] for p in plat) / len(plat)
            lon = sum(p[1] for p in plat) / len(plat)
        if lat is None or lon is None:
            continue

        want = True
        if sid_whitelist or name_whitelist:
            want = (pid in sid_whitelist) or (clean_station_name(name).lower() in name_whitelist)

        if want and bbox is not None and not within_bbox(lat, lon, bbox):
            if pid not in kids:
                want = False

        if not want:
            continue

        key = name
        if key in index:
            key = f"{name} ({pid})"
        index[key] = {"stop_id": pid, "name": key, "lat": lat, "lon": lon}

    return index

# ------------- Train feed (cached + merged) -------------
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

    total_entities = 0
    total_with_vehicle = 0
    total_with_pos = 0

    for url in URLS_VP:
        try:
            r = s.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(r.content)
            merged.entity.extend(feed.entity)

            ents = len(feed.entity)
            vehs = sum(1 for e in feed.entity if e.HasField("vehicle"))
            poss = sum(
                1 for e in feed.entity
                if e.HasField("vehicle") and e.vehicle.HasField("position")
                and hasattr(e.vehicle.position, "latitude")
                and hasattr(e.vehicle.position, "longitude")
            )
            total_entities += ents
            total_with_vehicle += vehs
            total_with_pos += poss
            print(f"[metroboard] fetched {url}: entities={ents}, vehicles={vehs}, vehicles_with_pos={poss}")
        except Exception as e:
            print(f"[metroboard] WARN: failed fetching {url}: {e}")

    print(f"[metroboard] merged feed: entities={total_entities}, vehicles={total_with_vehicle}, vehicles_with_pos={total_with_pos}")


    _FEED_CACHE = merged
    _FEED_TS = now
    return merged, 0.0

def collect_allowed_trains(feed: gtfs_realtime_pb2.FeedMessage, route_id_to_short: dict[str,str]) -> list[tuple[float,float]]:
    trains: list[tuple[float,float]] = []
    skipped_by_short = 0
    no_position = 0

    for e in feed.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        rid = v.trip.route_id if v.HasField("trip") else ""
        short = route_id_to_short.get(rid, "")
        if ROUTE_SHORT_ALLOW and short and short not in ROUTE_SHORT_ALLOW:
            skipped_by_short += 1
            continue
        if not v.HasField("position"):
            no_position += 1
            continue
        lat = getattr(v.position, "latitude", None)
        lon = getattr(v.position, "longitude", None)
        if lat is None or lon is None:
            no_position += 1
            continue
        trains.append((lat, lon))

    print(f"[metroboard] collect_allowed_trains: trains={len(trains)}, skipped_by_short={skipped_by_short}, no_position={no_position}")
    return trains


# ------------- FastAPI -------------
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

@app.on_event("startup")
def _startup():
    global ROUTE_ID_TO_SHORT, STATION_INDEX
    ROUTE_ID_TO_SHORT = load_route_shortnames("routes.txt")

    # Build allowed parents from static GTFS (mandatory)
    allowed_parents = require_allowed_parents(
        stops_path="stops.txt",
        routes_path="routes.txt",
        trips_path="trips.txt",
        stop_times_path="stop_times.txt",
        agency_path="agency.txt",
        route_types_allowed=(1,2),                 # Metro + Rail only
        agencies_allowed=AGENCY_NAME_ALLOW,        # Optional: tighten here
        route_short_allow=ROUTE_SHORT_ALLOW
    )

    # Optional: further restrict by user whitelist
    wl_path = os.getenv("STATION_WHITELIST_PATH", "stations_whitelist.txt")
    sid_w, name_w = load_whitelist(wl_path)

    STATION_INDEX = load_station_index(
        "stops.txt",
        bbox=SYD_BBOX,
        sid_whitelist=sid_w,
        name_whitelist=name_w,
        allowed_parents=allowed_parents
    )

    if not STATION_INDEX:
        raise RuntimeError("[metroboard] No stations loaded after filtering — check whitelist and GTFS files.")

    print(f"[metroboard] Loaded {len(STATION_INDEX)} stations (rail+metro only)")

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
    }

@app.get("/stations")
def stations():
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

    for name, meta in subset.items():
        slat, slon = meta["lat"], meta["lon"]
        best = None
        for (tlat, tlon) in trains:
            d = haversine_m(slat, slon, tlat, tlon)
            best = d if best is None else min(best, d)
        out[name] = status_from_distance(best)

    return JSONResponse(out)
