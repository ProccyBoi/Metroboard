import os, time, math, csv, random, typing as T
import requests
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.transit import gtfs_realtime_pb2
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_TOKEN = os.environ["TFNSW_API_TOKEN"]
URL_VP_ST = "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains"

# Distance thresholds (meters)
D_SOLID = 120
D_NEAR  = 500

# Route short-names you'll consider "in network"
ROUTE_SHORT_ALLOW = {"T1","T2","T3","T4","T5","T6","T7","T8","T9","CCN","SCO","BMT","SHL"}

# Caching + CORS
FEED_CACHE_SEC = int(os.getenv("FEED_CACHE_SEC", "3"))
CORS_ALLOW_ORIGIN = os.getenv("CORS_ALLOW_ORIGIN")

# Optional geographic guardrail (kept as a safety net, but we won't rely on it)
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

# ---------- Helpers ----------
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
    dlon = math.radians(lon2 - lon1)
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
    """
    Reads a simple text/CSV file listing stations that should be included.
    Accepts either parent station stop_ids or cleaned names (case-insensitive).
    - Lines starting with '#' are comments
    - CSV accepted (first token per line is used)
    Returns: (stop_id_whitelist, name_whitelist)
    """
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

def load_station_index(stops_path="stops.txt",
                       bbox: T.Optional[tuple[float,float,float,float]] = None,
                       sid_whitelist: set[str] = set(),
                       name_whitelist: set[str] = set()):
    """
    Build { name -> {stop_id, name, lat, lon} } only for stations present in the whitelist.
    Whitelist can list parent stop_ids OR station names (cleaned, case-insensitive).
    If whitelist sets are empty, falls back to 'all parents' (optionally filtered by bbox).
    """
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
                # Parent station row
                parents[stop_id] = {"stop_id": stop_id, "raw_name": name_raw, "lat": lat, "lon": lon}
            else:
                if parent and lat is not None and lon is not None:
                    if (bbox is None) or within_bbox(lat, lon, bbox):
                        kids.setdefault(parent, []).append((lat, lon))

    index: dict[str, dict] = {}
    for pid, meta in parents.items():
        raw_name = meta["raw_name"]
        name = clean_station_name(raw_name)
        lat, lon = meta["lat"], meta["lon"]

        # Use averaged child coords if parent missing/blank
        if (lat is None or lon is None) and pid in kids:
            plat = kids[pid]
            if plat:
                lat = sum(p[0] for p in plat) / len(plat)
                lon = sum(p[1] for p in plat) / len(plat)

        if lat is None or lon is None:
            continue

        # If whitelist provided, enforce it:
        want = True
        if sid_whitelist or name_whitelist:
            want = (pid in sid_whitelist) or (clean_station_name(name).lower() in name_whitelist)

        # If still wanted, optionally keep a loose bbox guardrail to exclude obvious outliers
        if want and bbox is not None and not within_bbox(lat, lon, bbox):
            # If we averaged from in-bbox platforms, it's still fine; otherwise skip
            if pid not in kids:
                want = False

        if not want:
            continue

        key = name
        if key in index:
            key = f"{name} ({pid})"
        index[key] = {"stop_id": pid, "name": key, "lat": lat, "lon": lon}

    return index

# ---------- Train feed (cached) ----------
_FEED_CACHE = None  # type: ignore
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
    r = s.get(URL_VP_ST, headers=headers, timeout=20)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    _FEED_CACHE = feed
    _FEED_TS = now
    return feed, 0.0

def collect_allowed_trains(feed, route_id_to_short: dict[str,str]) -> list[tuple[float,float]]:
    trains: list[tuple[float,float]] = []
    for e in feed.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        rid = v.trip.route_id if v.HasField("trip") else ""
        short = route_id_to_short.get(rid, "")
        if short and short not in ROUTE_SHORT_ALLOW:
            continue
        if not v.HasField("position"):
            continue
        lat = getattr(v.position, "latitude", None)
        lon = getattr(v.position, "longitude", None)
        if lat is None or lon is None:
            continue
        trains.append((lat, lon))
    return trains

# ---------- FastAPI ----------
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

    # Whitelist support
    wl_path = os.getenv("STATION_WHITELIST_PATH", "stations_whitelist.txt")
    sid_w, name_w = load_whitelist(wl_path)  # empty sets if file missing

    # Build station index restricted to whitelist (if provided)
    STATION_INDEX = load_station_index(
        "stops.txt",
        bbox=SYD_BBOX,               # soft guardrail
        sid_whitelist=sid_w,
        name_whitelist=name_w
    )

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
        "cache_sec": FEED_CACHE_SEC,
        "whitelist_active": True
    }

@app.get("/stations")
def stations():
    return JSONResponse([STATION_INDEX[k] for k in sorted(STATION_INDEX.keys())])

@app.get("/states")
def states(only: T.Optional[str] = Query(None, description="Comma-separated station names to restrict the output")):
    # Select subset
    subset = STATION_INDEX
    if only:
        wanted = [n.strip() for n in only.split(",") if n.strip()]
        subset = {n: STATION_INDEX[n] for n in wanted if n in STATION_INDEX}
        if not subset:
            return JSONResponse({})

    # Fetch trains (cached)
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
