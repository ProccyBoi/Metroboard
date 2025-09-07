# app.py — FastAPI proxy for Metroboard (VehiclePositions -> per-station states JSON)
# Exposes:
#   GET /           -> {"ok": true, "endpoints": ["/states", "/healthz", "/debug"]}
#   GET /healthz    -> {"status": "ok"}
#   GET /debug      -> resolved station coords (to verify IDs)
#   GET /states     -> {"Eastwood":0..3, "Epping":0..3, ...}
#
# Env:
#   TFNSW_API_TOKEN  (Transport NSW API key)
#
# Files baked into the image:
#   routes.txt, stops.txt  (from the same NSW "Timetables – For Realtime v2" bundle)

import os, time, math, csv, random, requests
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from google.transit import gtfs_realtime_pb2
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_TOKEN = os.environ["TFNSW_API_TOKEN"]
URL_VP_ST = "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains"

# Your target stations (parent station IDs are fine — we resolve to child coords if needed)
STATIONS = {
    "Eastwood":      "216020",
    "Epping":        "212110",
    "Cheltenham":    "211920",
    "Beecroft":      "211910",
    "PennantHills":  "212020",
}

# Distance thresholds (meters)
D_SOLID = 120   # solid if train roughly at the station
D_NEAR  = 500   # slow blink if within 500 m

# Allowed lines
ROUTE_SHORT_ALLOW = {"T1","T2","T3","T4","T5","T6","T7","T8","T9","CCN","SCO","BMT","SHL"}

def status_from_distance(m):
    if m is None: return 0
    if m <= D_SOLID: return 3
    if m <= D_NEAR:  return 1
    return 0

def load_route_shortnames(path="routes.txt"):
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rid = (row.get("route_id") or "").strip()
            rs  = (row.get("route_short_name") or "").strip()
            if rid:
                out[rid] = rs
    return out

def load_station_coords(path="stops.txt"):
    """
    Returns:
      coords: dict stop_id -> (lat, lon) for any row that has coords
      children_by_parent: dict parent_station -> list[(lat, lon)]
    """
    coords = {}
    children_by_parent = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sid = (row.get("stop_id") or "").strip()
            if not sid:
                continue
            ps  = (row.get("parent_station") or "").strip()
            lat = row.get("stop_lat"); lon = row.get("stop_lon")
            try:
                if lat and lon:
                    latf, lonf = float(lat), float(lon)
                    coords[sid] = (latf, lonf)
                    if ps:
                        children_by_parent.setdefault(ps, []).append((latf, lonf))
            except ValueError:
                pass
    return coords, children_by_parent

def resolve_coords(stop_id: str):
    """Exact stop coords if present; else average of child coords for a parent."""
    c = STOP_COORDS.get(stop_id)
    if c:
        return c
    kids = CHILDREN_BY_PARENT.get(stop_id)
    if kids:
        lat = sum(k[0] for k in kids) / len(kids)
        lon = sum(k[1] for k in kids) / len(kids)
        return (lat, lon)
    return None

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def session():
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=0.6,
                  status_forcelist=(500,502,503,504),
                  allowed_methods=frozenset(["GET"]),
                  raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

def fetch_vehicle_positions():
    headers = {
        "Authorization": f"apikey {API_TOKEN}",
        "Accept": "application/x-protobuf",
        "Cache-Control": "no-cache",
        "User-Agent": "metroboard/1.0",
    }
    s = session()
    time.sleep(random.uniform(0, 0.3))  # jitter
    r = s.get(URL_VP_ST, headers=headers, timeout=20)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    return feed

def collect_allowed_trains(feed):
    """Return list of (lat, lon) for trains on allowed lines that have positions."""
    trains = []
    for e in feed.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        rid = v.trip.route_id if v.HasField("trip") else ""
        short = ROUTE_ID_TO_SHORT.get(rid, "")
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

# ---------- App ----------
app = FastAPI()

ROUTE_ID_TO_SHORT = {}
STOP_COORDS = {}
CHILDREN_BY_PARENT = {}

@app.on_event("startup")
def _startup():
    global ROUTE_ID_TO_SHORT, STOP_COORDS, CHILDREN_BY_PARENT
    ROUTE_ID_TO_SHORT = load_route_shortnames("routes.txt")
    STOP_COORDS, CHILDREN_BY_PARENT = load_station_coords("stops.txt")

@app.get("/")
def root():
    return {"ok": True, "endpoints": ["/states", "/healthz", "/debug"]}

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/debug")
def debug():
    return {"stations_resolved": {name: resolve_coords(sid) for name, sid in STATIONS.items()}}

@app.get("/states")
def states():
    feed = fetch_vehicle_positions()
    trains = collect_allowed_trains(feed)

    out = {}
    for name, sid in STATIONS.items():
        sloc = resolve_coords(sid)
        if not sloc:
            out[name] = 0
            continue
        slat, slon = sloc
        best = None
        for (tlat, tlon) in trains:
            d = haversine_m(slat, slon, tlat, tlon)
            best = d if best is None else min(best, d)
        out[name] = status_from_distance(best) if best is not None else 0

    return JSONResponse(out)
