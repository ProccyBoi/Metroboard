import os, time, math, csv, random, requests
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from google.transit import gtfs_realtime_pb2
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_TOKEN   = os.environ["TFNSW_API_TOKEN"]
URL_VP_ST   = "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains"

# Keep your same five parent-station IDs
STATIONS = {
    "Eastwood":      "216020",
    "Epping":        "212110",
    "Cheltenham":    "211920",
    "Beecroft":      "211910",
    "PennantHills":  "212020",
}

D_SOLID = 120
D_NEAR  = 500
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
            rid = row.get("route_id",""); rs = row.get("route_short_name","")
            if rid: out[rid] = rs
    return out

def load_station_coords(path="stops.txt"):
    coords = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sid = row.get("stop_id","")
            if not sid: continue
            try:
                coords[sid] = (float(row["stop_lat"]), float(row["stop_lon"]))
            except: pass
    return coords

def haversine_m(lat1, lon1, lat2, lon2):
    R=6371000.0
    from math import radians, sin, cos, asin, sqrt
    dlat=radians(lat2-lat1); dlon=radians(lon2-lon1)
    a=sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    return 2*R*asin(sqrt(a))

def session():
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=0.6,
                  status_forcelist=(500,502,503,504),
                  allowed_methods=frozenset(["GET"]))
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
    time.sleep(random.uniform(0,0.3))
    r = s.get(URL_VP_ST, headers=headers, timeout=20)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    return feed

ROUTE_ID_TO_SHORT = None
STOP_COORDS = None

app = FastAPI()

@app.on_event("startup")
def _load_static():
    global ROUTE_ID_TO_SHORT, STOP_COORDS
    ROUTE_ID_TO_SHORT = load_route_shortnames("routes.txt")
    STOP_COORDS       = load_station_coords("stops.txt")

@app.get("/states")
def states():
    feed = fetch_vehicle_positions()

    # gather allowed train positions
    trains = []
    for e in feed.entity:
        if not e.HasField("vehicle"): continue
        v = e.vehicle
        rid = v.trip.route_id if v.HasField("trip") else ""
        short = ROUTE_ID_TO_SHORT.get(rid, "")
        if short and short not in ROUTE_SHORT_ALLOW: continue
        if not v.HasField("position"): continue
        lat = getattr(v.position, "latitude", None)
        lon = getattr(v.position, "longitude", None)
        if lat is None or lon is None: continue
        trains.append((lat, lon))

    out = {}
    for name, sid in STATIONS.items():
        sloc = STOP_COORDS.get(sid)
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
