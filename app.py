# app.py — Metroboard backend (Rail+Metro) with line separation
import os, time, csv, json, re, typing as T, random, requests
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.transit import gtfs_realtime_pb2 as rt
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------- Config helpers ----------
def _get_csv_env(*names: str) -> set[str]:
    raw = ""
    for n in names:
        v = os.getenv(n)
        if v:
            raw = v
            break
    return {s.strip() for s in raw.split(",") if s.strip()}

def _get_json_env(*names: str, default="{}"):
    raw = None
    for n in names:
        v = os.getenv(n)
        if v:
            raw = v
            break
    if raw is None:
        raw = default
    try:
        return json.loads(raw)
    except Exception:
        return json.loads(default)

# ---------- Env config ----------
API_TOKEN = os.getenv("TFNSW_API_KEY") or os.getenv("TFNSW_API_TOKEN")
if not API_TOKEN:
    raise RuntimeError("Missing TFNSW_API_KEY")

URLS_VP = [
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
]

D_SOLID = int(os.getenv("MB_D_SOLID", "200"))
D_NEAR  = int(os.getenv("MB_D_NEAR",  "800"))
D_FAR   = int(os.getenv("MB_D_FAR",   "1500"))

AGENCY_ALLOW      = _get_csv_env("AGENCY_ALLOW", "MB_AGENCIES")
ROUTE_SHORT_ALLOW = {s.upper() for s in _get_csv_env("ROUTE_SHORT_ALLOW", "MB_ROUTE_SHORT_ALLOW")}
ROUTE_TYPES_ALLOW = {int(x) for x in _get_csv_env("ROUTE_TYPES_ALLOW")} or {1, 2}

ROUTE_SHORT_OVERRIDES_PATH = os.getenv("ROUTE_SHORT_OVERRIDES_PATH", "route_overrides.csv")
ROUTE_SHORT_OVERRIDES_JSON = os.getenv("ROUTE_SHORT_OVERRIDES_JSON", "")

LINE_ALIASES = _get_json_env("LINE_ALIASES_JSON", default="{}")
if isinstance(LINE_ALIASES, dict):
    LINE_ALIASES = {str(k).upper(): [str(v).upper() for v in vals] for k, vals in LINE_ALIASES.items()}
else:
    LINE_ALIASES = {}
STRICT_LINE_MATCH = os.getenv("STRICT_LINE_MATCH", "1").lower() in ("1", "true", "yes")

FEED_CACHE_SEC = int(os.getenv("FEED_CACHE_SEC", "3"))
CORS_ALLOW_ORIGIN = os.getenv("CORS_ALLOW_ORIGIN")
MB_VERBOSE = os.getenv("MB_VERBOSE", "1") == "1"
MB_FORCE_DISTANCE = os.getenv("MB_FORCE_DISTANCE", "0") == "1"

# ---------- Utils ----------
EXCLUDE_SUB = ("wharf","light rail","b-line","interchange","motorway"," m2")
SHORT_RE = re.compile(r'\b([TM]\d{1,2})\b', re.I)

def _sess():
    s=requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=4, backoff_factor=0.6,
        status_forcelist=(500,502,503,504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False)))
    return s

def vlog(*a):
    if MB_VERBOSE: print("[metroboard]",*a)

def _valid_ll(x,y):
    try: x=float(x); y=float(y)
    except: return False
    return -90<=x<=90 and -180<=y<=180

def hav_m(a,b,c,d):
    try: a=float(a); b=float(b); c=float(c); d=float(d)
    except: return float("inf")
    if not (_valid_ll(a,b) and _valid_ll(c,d)): return float("inf")
    from math import radians,sin,cos,asin,sqrt
    R=6371000.0; da=radians(c-a); db=radians(d-b)
    t=sin(da/2)**2+cos(radians(a))*cos(radians(c))*sin(db/2)**2
    return 2*R*asin(sqrt(max(0.0,min(1.0,t))))

def status_from_dist(m):
    if m is None: return 0
    if m<=D_SOLID: return 3
    if m<=D_NEAR : return 2
    if m<=D_FAR  : return 1
    return 0

def clean_name(n:str)->str:
    n=(n or "").strip()
    for suf in (" Station"," Railway Station"):
        if n.endswith(suf): n=n[:-len(suf)]
    return n

def is_num_parent(s:str)->bool: return s.isdigit()

# ---------- Route/GTFS loaders ----------
def load_route_overrides(path:str,json_env:str)->dict[str,str]:
    out={}
    try:
        if path and os.path.exists(path):
            with open(path,encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    rid=(r.get("route_id") or "").strip()
                    rs=(r.get("route_short_name") or "").strip().upper()
                    if rid and rs: out[rid]=rs
            vlog(f"route_overrides: {len(out)} from file")
    except Exception as e: vlog("WARN override file:",e)
    if json_env:
        try:
            obj=json.loads(json_env)
            if isinstance(obj,dict):
                for k,v in obj.items():
                    k=(k or "").strip(); v=(v or "").strip().upper()
                    if k and v: out[k]=v
            vlog("route_overrides: merged env JSON")
        except Exception as e: vlog("WARN override env:",e)
    return out

def derive_short(rs,rl):
    rs=(rs or "").strip()
    if rs: return rs.upper()
    rl=(rl or "").strip(); m=SHORT_RE.search(rl)
    return (m.group(1).upper() if m else "")

def load_route_shortnames(routes_path="routes.txt")->dict[str,str]:
    ov=load_route_overrides(ROUTE_SHORT_OVERRIDES_PATH,ROUTE_SHORT_OVERRIDES_JSON); out={}
    with open(routes_path,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid=(r.get("route_id") or "").strip()
            if not rid: continue
            rs=ov.get(rid) or derive_short(r.get("route_short_name"),r.get("route_long_name"))
            if rs: out[rid]=rs
    vlog(f"route_short: {len(out)} (ov={len(ov)})"); return out

def require_allowed_parents(stops="stops.txt",routes="routes.txt",trips="trips.txt",
                            stop_times="stop_times.txt",agency="agency.txt",
                            types=(1,2)):
    ag_name={}
    with open(agency,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ag_name[(r.get("agency_id") or "").strip()]=(r.get("agency_name") or "").strip()
    route_meta={}
    with open(routes,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid=(r.get("route_id") or "").strip()
            try: rtype=int((r.get("route_type") or "3").strip() or "3")
            except: rtype=3
            route_meta[rid]=(rtype,ag_name.get((r.get("agency_id") or "").strip(),""))
    trip_route={}
    with open(trips,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f): trip_route[(r.get("trip_id") or "").strip()]=(r.get("route_id") or "").strip()
    stops_map,parents={},set()
    with open(stops,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid=(r.get("stop_id") or "").strip(); stops_map[sid]=r
            if (r.get("location_type") or "0").strip()=="1": parents.add(sid)
    def parent_of(sid):
        row=stops_map.get(sid); 
        if not row: return None
        p=(row.get("parent_station") or "").strip()
        return p or (sid if sid in parents else None)
    allowed=set()
    with open(stop_times,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid=trip_route.get((r.get("trip_id") or "").strip(),"")
            rtype,_=route_meta.get(rid,(None,""))
            if rtype is None or rtype not in types: continue
            p=parent_of((r.get("stop_id") or "").strip())
            if p and is_num_parent(p): allowed.add(p)
    return allowed

def require_allowed_pairs(stops="stops.txt",routes="routes.txt",trips="trips.txt",
                          stop_times="stop_times.txt",agency="agency.txt",
                          types=(1,2),allowed_parents_for_fallback=None):
    rt_meta={}
    with open(routes,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid=(r.get("route_id") or "").strip()
            try: rtype=int((r.get("route_type") or "3").strip() or "3")
            except: rtype=3
            rt_meta[rid]=rtype
    trip_route={}
    with open(trips,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f): trip_route[(r.get("trip_id") or "").strip()]=(r.get("route_id") or "").strip()
    stops_map,parents={},set()
    with open(stops,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid=(r.get("stop_id") or "").strip(); stops_map[sid]=r
            if (r.get("location_type") or "0").strip()=="1": parents.add(sid)
    def parent_of(sid):
        row=stops_map.get(sid); 
        if not row: return None
        p=(row.get("parent_station") or "").strip()
        return p or (sid if sid in parents else None)
    rshort=load_route_shortnames(routes)
    pairs=set()
    with open(stop_times,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid=trip_route.get((r.get("trip_id") or "").strip(),"")
            rtype=rt_meta.get(rid)
            if rtype is None or rtype not in types: continue
            rs=(rshort.get(rid,"") or "").upper()
            if not rs: continue
            if ROUTE_SHORT_ALLOW and rs not in ROUTE_SHORT_ALLOW: continue
            p=parent_of((r.get("stop_id") or "").strip())
            if p and is_num_parent(p): pairs.add((p,rs))
    return pairs

def route_suffix(rs:str)->str:
    rs=(rs or "").upper()
    if rs.startswith("T"): return rs[1:]
    if rs.startswith("M"): return "M"
    return rs or "?"

def load_station_index(stops="stops.txt",allowed_parents=set(),allowed_pairs=set()):
    parents={}
    with open(stops,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            lt=(r.get("location_type") or "0").strip(); sid=(r.get("stop_id") or "").strip()
            name=(r.get("stop_name") or "").strip(); lat_s,lon_s=r.get("stop_lat"),r.get("stop_lon")
            lat=float(lat_s) if lat_s else None; lon=float(lon_s) if lon_s else None
            if lt=="1" and sid in allowed_parents:
                parents[sid]={"name":name,"lat":lat,"lon":lon}
    by_parent={}
    for pid,rs in allowed_pairs: by_parent.setdefault(pid,set()).add(rs)
    idx={}
    for pid,meta in parents.items():
        base=clean_name(meta["name"])
        lat,lon=meta["lat"],meta["lon"]
        for rs in sorted(by_parent.get(pid,set())):
            # expand aliases only at station index creation time
            for rs_eff in LINE_ALIASES.get(rs,[rs]):
                suf=route_suffix(rs_eff); key=f"{base}{suf}"
                if key in idx: key=f"{base}{suf} ({pid})"
                idx[key]={"stop_id":pid,"name":key,"lat":lat,"lon":lon,
                          "route_short":rs_eff,"base_name":base}
    return idx

def build_child_to_parent(stops="stops.txt"):
    out={}
    with open(stops,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid=(r.get("stop_id") or "").strip()
            pid=(r.get("parent_station") or "").strip()
            lt=(r.get("location_type") or "0").strip()
            if sid and pid and is_num_parent(pid): out[sid]=pid
            if sid and lt=="1" and is_num_parent(sid): out.setdefault(sid,sid)
    return out

# ---------- Feed ----------
_FEED_CACHE=None; _FEED_TS=0.0
def fetch_vp_cached():
    global _FEED_CACHE,_FEED_TS
    now=time.time()
    if _FEED_CACHE is not None and (now-_FEED_TS)<FEED_CACHE_SEC: return _FEED_CACHE,(now-_FEED_TS)
    hdr={"Authorization":f"apikey {API_TOKEN}","Accept":"application/x-protobuf","Cache-Control":"no-cache"}
    s=_sess(); time.sleep(random.uniform(0,0.25))
    merged=rt.FeedMessage(); merged.header.gtfs_realtime_version="2.0"
    for url in URLS_VP:
        try:
            r=s.get(url,headers=hdr,timeout=20); r.raise_for_status()
            feed=rt.FeedMessage(); feed.ParseFromString(r.content)
            merged.entity.extend(feed.entity)
        except Exception as e: vlog("WARN fetch",url,e)
    _FEED_CACHE=merged; _FEED_TS=now; return merged,0.0

def collect_trains(feed,route_id_to_short):
    trains=[]; by_route={}
    for e in feed.entity:
        if not e.HasField("vehicle"): continue
        v=e.vehicle
        rid=v.trip.route_id if v.HasField("trip") else ""
        rs=(route_id_to_short.get(rid,"") or "").upper() or None
        if ROUTE_SHORT_ALLOW and rs and rs not in ROUTE_SHORT_ALLOW: continue
        sid=getattr(v,"stop_id","")
        st=int(getattr(v,"current_status",rt.VehiclePosition.IN_TRANSIT_TO))
        lat=getattr(v.position,"latitude",None) if v.HasField("position") else None
        lon=getattr(v.position,"longitude",None) if v.HasField("position") else None
        trains.append((lat,lon,rs,sid,st))
        if rs: by_route[rs]=by_route.get(rs,0)+1
    return trains,by_route

# ---------- API ----------
app=FastAPI()
if CORS_ALLOW_ORIGIN:
    app.add_middleware(CORSMiddleware,allow_origins=[o.strip() for o in CORS_ALLOW_ORIGIN.split(",")],
                       allow_credentials=True,allow_methods=["*"],allow_headers=["*"])

ROUTE_ID_TO_SHORT={}; STATION_INDEX={}; CHILD_TO_PARENT={}
@app.on_event("startup")
def _startup():
    global ROUTE_ID_TO_SHORT,STATION_INDEX,CHILD_TO_PARENT
    ROUTE_ID_TO_SHORT=load_route_shortnames("routes.txt")
    allowed_parents=require_allowed_parents(types=tuple(sorted(ROUTE_TYPES_ALLOW)))
    allowed_pairs=require_allowed_pairs(allowed_parents_for_fallback=allowed_parents,
                                        types=tuple(sorted(ROUTE_TYPES_ALLOW)))
    STATION_INDEX=load_station_index(allowed_parents=allowed_parents,allowed_pairs=allowed_pairs)
    CHILD_TO_PARENT=build_child_to_parent("stops.txt")
    if not STATION_INDEX: raise RuntimeError("No stations after filtering")
    vlog(f"stations loaded: {len(STATION_INDEX)}")

@app.get("/")
def root(): return {"ok":True,"endpoints":["/states","/stations","/healthz","/debug","/trains"]}

@app.get("/healthz")
def healthz():
    age=None if _FEED_TS==0 else (time.time()-_FEED_TS)
    return {"status":"ok","stations":len(STATION_INDEX),"trains_last_fetch_s":age}

@app.get("/debug")
def debug():
    feed,_=fetch_vp_cached()
    trains,by_route=collect_trains(feed,ROUTE_ID_TO_SHORT)
    return {
        "stations_count":len(STATION_INDEX),
        "allowed_routes":sorted(ROUTE_SHORT_ALLOW) if ROUTE_SHORT_ALLOW else "all",
        "allowed_agencies":sorted(AGENCY_ALLOW) if AGENCY_ALLOW else "all",
        "cache_sec":FEED_CACHE_SEC,"feeds":URLS_VP,
        "current_trains":{"total":len(trains),"by_route":by_route}
    }

@app.get("/trains")
def trains():
    feed,age=fetch_vp_cached()
    trains,by_route=collect_trains(feed,ROUTE_ID_TO_SHORT)
    out=[{"route":rs,"lat":lat,"lon":lon,"stop_id":sid,"status":st,
          "parent_station":CHILD_TO_PARENT.get(sid,sid) if sid else None}
         for lat,lon,rs,sid,st in trains[:50]]
    return {"feed_age_seconds":age,"total_trains":len(trains),"sample_trains":out}

@app.get("/stations")
def stations():
    return JSONResponse([STATION_INDEX[k] for k in sorted(STATION_INDEX.keys())])

@app.get("/states")
def states(only: T.Optional[str]=Query(None)):
    subset = STATION_INDEX if not only else {
        n: STATION_INDEX[n] for n in [x.strip() for x in only.split(",") if x.strip()] if n in STATION_INDEX
    }
    if not subset: return JSONResponse({})

    feed,_ = fetch_vp_cached()
    trains,by_route = collect_trains(feed,ROUTE_ID_TO_SHORT)
    if MB_VERBOSE: vlog(f"processing trains={len(trains)} stations={len(subset)}")
    if not trains: return JSONResponse({k:0 for k in subset.keys()})

    # Group the vehicles by their resolved route short name (rs), plus None bucket
    grouped: dict[T.Optional[str], list[tuple]] = {}
    for t in trains:
        grouped.setdefault(t[2], []).append(t)

    VP_INCOMING = rt.VehiclePosition.INCOMING_AT
    VP_STOPPED  = rt.VehiclePosition.STOPPED_AT

    out={}; matches=0; dist_fb=0
    for name,meta in subset.items():
        pid=meta["stop_id"]; slat,slon=meta["lat"],meta["lon"]; rs=(meta.get("route_short") or "").upper()

        # honor STRICT_LINE_MATCH; optionally mix in alias lines
        rs_list = [rs]
        if not STRICT_LINE_MATCH and rs:
            rs_list = LINE_ALIASES.get(rs, [rs])

        best=0; best_d=None
        # examine trains only for this line (and alias lines if lenient) + unknown-route trains (rs None)
        cand_trains = []
        for rsi in rs_list:
            cand_trains.extend(grouped.get(rsi, []))
        cand_trains.extend(grouped.get(None, []))

        for (tlat,tlon,trs,child_sid,st) in cand_trains:
            # parent match (exact)
            if not MB_FORCE_DISTANCE and child_sid:
                parent = CHILD_TO_PARENT.get(child_sid) or (child_sid if is_num_parent(child_sid) else None)
                if parent == pid:
                    if st == VP_STOPPED: best=3; matches+=1; break
                    elif st == VP_INCOMING: best=max(best,2); matches+=1
            # distance fallback
            if best<3 and tlat is not None and tlon is not None:
                d = hav_m(slat,slon,tlat,tlon)
                if best_d is None or d<best_d: best_d=d

        if best==0 and best_d is not None:
            best = status_from_dist(best_d); dist_fb += (1 if best>0 else 0)
        out[name]=best

    if MB_VERBOSE:
        act=sum(1 for v in out.values() if v>0)
        vlog(f"result active={act}/{len(subset)} matches={matches} distance_fb={dist_fb}")
    return JSONResponse(out)
