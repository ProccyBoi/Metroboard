# app.py — Metroboard backend (Rail+Metro) — compact + resilient
import os,time,csv,json,re,typing as T,random,requests
from fastapi import FastAPI,Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.transit import gtfs_realtime_pb2 as rt
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_TOKEN=os.environ["TFNSW_API_TOKEN"]
URLS_VP=[ # v2 endpoints
  "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
  "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
]
# Distance thresholds (m) for fallback
D_SOLID=int(os.getenv("MB_D_SOLID","200"))
D_NEAR =int(os.getenv("MB_D_NEAR","800"))
D_FAR  =int(os.getenv("MB_D_FAR","1500"))
# Allowlists (empty=set() means all)
AGENCY_ALLOW={a.strip() for a in os.getenv("MB_AGENCIES","").split(",") if a.strip()}
ROUTE_SHORT_ALLOW={s.strip().upper() for s in os.getenv("MB_ROUTE_SHORT_ALLOW","").split(",") if s.strip()}
# Route short-name overrides
ROUTE_SHORT_OVERRIDES_PATH=os.getenv("ROUTE_SHORT_OVERRIDES_PATH","route_overrides.csv")
ROUTE_SHORT_OVERRIDES_JSON=os.getenv("ROUTE_SHORT_OVERRIDES_JSON","")
# Caching/CORS/logging
FEED_CACHE_SEC=int(os.getenv("FEED_CACHE_SEC","3"))
CORS_ALLOW_ORIGIN=os.getenv("CORS_ALLOW_ORIGIN")
MB_VERBOSE=os.getenv("MB_VERBOSE","1")=="1"
MB_FORCE_DISTANCE=os.getenv("MB_FORCE_DISTANCE","0")=="1"
# BBox guardrail (None disables)
def _bbox_env():
  if os.getenv("SYD_BBOX_DISABLE")=="1": return None
  s=os.getenv("SYD_BBOX"); 
  if not s: return (-34.2,150.5,-33.4,151.4)
  try: a,b,c,d=[float(x) for x in s.split(",")]; return (a,b,c,d)
  except: return (-34.2,150.5,-33.4,151.4)
SYD_BBOX=_bbox_env()
def vlog(*a): 
  if MB_VERBOSE: print("[metroboard]",*a)

EXCLUDE_SUB=("wharf","light rail","b-line","interchange","motorway"," m2")
SHORT_RE=re.compile(r'\b([TM]\d{1,2})\b',re.I)

def _sess():
  s=requests.Session(); s.mount("https://",HTTPAdapter(max_retries=Retry(total=4,backoff_factor=0.6,status_forcelist=(500,502,503,504),allowed_methods=frozenset(["GET"]),raise_on_status=False))); 
  return s

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

def within_bbox(lat,lon,bbox): S,W,N,E=bbox; return S<=lat<=N and W<=lon<=E
def is_num_parent(s:str)->bool: return s.isdigit()
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

def _is_stationish(n:str)->bool:
  n=(n or "").strip().lower()
  if any(s in n for s in EXCLUDE_SUB): return False
  return n.endswith(" station") or n.endswith(" railway station")

def load_route_overrides(path:str,json_env:str)->dict[str,str]:
  out={}
  try:
    if path and os.path.exists(path):
      with open(path,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
          rid=(r.get("route_id") or "").strip(); rs=(r.get("route_short_name") or "").strip().upper()
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
  rs=(rs or "").strip(); 
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

def require_allowed_parents(stops="stops.txt",routes="routes.txt",trips="trips.txt",stop_times="stop_times.txt",agency="agency.txt",types=(1,2)):
  miss=[p for p in (stops,routes,trips,stop_times,agency) if not os.path.exists(p)]
  if miss: raise RuntimeError("Missing GTFS: "+", ".join(miss))
  ag_name={}
  with open(agency,encoding="utf-8-sig") as f:
    for r in csv.DictReader(f): ag_name[(r.get("agency_id") or "").strip()]=(r.get("agency_name") or "").strip()
  route_meta={}
  with open(routes,encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
      rid=(r.get("route_id") or "").strip()
      try: rtype=int((r.get("route_type") or "3").strip() or "3")
      except: rtype=3
      route_meta[rid]=(rtype,(r.get("route_short_name") or "").strip(),ag_name.get((r.get("agency_id") or "").strip(),""))
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
  allowed=set(); seen_types=set(); seen_ag=set()
  with open(stop_times,encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
      rid=trip_route.get((r.get("trip_id") or "").strip(),"")
      rtype,_,ag=route_meta.get(rid,(None,"",""))
      if rtype is None: continue
      seen_types.add(rtype); seen_ag.add(ag)
      if types and rtype not in types: continue
      if AGENCY_ALLOW and ag not in AGENCY_ALLOW: continue
      p=parent_of((r.get("stop_id") or "").strip())
      if p and is_num_parent(p): allowed.add(p)
  vlog("routes types seen",sorted(seen_types),"agencies",sorted(seen_ag))
  if allowed: vlog(f"parents by trips: {len(allowed)}"); return allowed
  # fallback: names
  fb=set()
  with open(stops,encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
      if (r.get("location_type") or "0").strip()!="1": continue
      pid=(r.get("stop_id") or "").strip()
      if not is_num_parent(pid): continue
      if _is_stationish((r.get("stop_name") or "")): fb.add(pid)
  if not fb: raise RuntimeError("No parent stations via fallback.")
  vlog(f"parents by names: {len(fb)}"); return fb

def _known_tm(route_id_to_short:dict[str,str])->set[str]:
  out=set()
  for rs in route_id_to_short.values():
    u=(rs or "").upper().strip()
    if re.fullmatch(r"[TM]\d{1,2}",u): out.add(u)
  return out

def require_allowed_pairs(stops="stops.txt",routes="routes.txt",trips="trips.txt",stop_times="stop_times.txt",agency="agency.txt",types=(1,2),allowed_parents_for_fallback: T.Optional[set[str]]=None):
  ag_name={}
  with open(agency,encoding="utf-8-sig") as f:
    for r in csv.DictReader(f): ag_name[(r.get("agency_id") or "").strip()]=(r.get("agency_name") or "").strip()
  rt_meta={}
  with open(routes,encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
      rid=(r.get("route_id") or "").strip()
      try: rtype=int((r.get("route_type") or "3").strip() or "3")
      except: rtype=3
      rt_meta[rid]=(rtype,ag_name.get((r.get("agency_id") or "").strip(),""),(r.get("route_long_name") or "").strip())
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
      rtype,ag,_=rt_meta.get(rid,(None,"",""))
      if rtype is None or (types and rtype not in types): continue
      if AGENCY_ALLOW and ag not in AGENCY_ALLOW: continue
      rs=(rshort.get(rid,"") or "").upper()
      if not rs: continue
      if ROUTE_SHORT_ALLOW and rs not in ROUTE_SHORT_ALLOW: continue
      p=parent_of((r.get("stop_id") or "").strip())
      if p and is_num_parent(p): pairs.add((p,rs))
  if pairs: vlog(f"pairs strict: {len(pairs)}"); return pairs
  # permissive fallback
  if os.getenv("MB_PERMISSIVE_PAIRS","1")!="0":
    known=_known_tm(rshort)
    if ROUTE_SHORT_ALLOW: known&={x.upper() for x in ROUTE_SHORT_ALLOW}
    if not known: vlog("WARN: no T*/M* for fallback"); return set()
    if not allowed_parents_for_fallback:
      allowed_parents_for_fallback={sid for sid,row in stops_map.items() if (row.get("location_type") or "0").strip()=="1" and is_num_parent(sid)}
    fan={(pid,rs) for pid in allowed_parents_for_fallback for rs in known}
    vlog(f"pairs fallback: {len(fan)}"); return fan
  raise RuntimeError("No station×route pairs; supply overrides or enable fallback.")

def route_suffix(rs:str)->str:
  rs=(rs or "").upper()
  if rs.startswith("T") and len(rs)>=2: return rs[1:]
  if rs.startswith("M"): return "M"
  return rs or "?"

def load_station_index(stops="stops.txt",bbox=SYD_BBOX,allowed_parents:set[str]=set(),allowed_pairs:set[tuple[str,str]]=set()):
  parents,children={},{}
  with open(stops,encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
      lt=(r.get("location_type") or "0").strip(); sid=(r.get("stop_id") or "").strip()
      name=(r.get("stop_name") or "").strip(); lat_s,lon_s=r.get("stop_lat"),r.get("stop_lon")
      lat=float(lat_s) if lat_s else None; lon=float(lon_s) if lon_s else None
      if lt=="1":
        if is_num_parent(sid) and sid in allowed_parents and not any(x in name.lower() for x in EXCLUDE_SUB):
          parents[sid]={"name":name,"lat":lat,"lon":lon}
      else:
        p=(r.get("parent_station") or "").strip()
        if p and lat is not None and lon is not None:
          if (bbox is None) or within_bbox(lat,lon,bbox): children.setdefault(p,[]).append((lat,lon))
  by_parent={}
  for pid,rs in allowed_pairs: by_parent.setdefault(pid,set()).add(rs)
  idx={}
  for pid,meta in parents.items():
    base=clean_name(meta["name"])
    lat,lon=meta["lat"],meta["lon"]
    if (lat is None or lon is None) and pid in children and children[pid]:
      lat=sum(t[0] for t in children[pid])/len(children[pid]); lon=sum(t[1] for t in children[pid])/len(children[pid])
    if lat is None or lon is None: continue
    if bbox is not None and not within_bbox(lat,lon,bbox) and pid not in children: continue
    for rs in sorted(by_parent.get(pid,set())):
      suf=route_suffix(rs); key=f"{base}{suf}"
      if key in idx: key=f"{base}{suf} ({pid})"
      idx[key]={"stop_id":pid,"name":key,"lat":lat,"lon":lon,"route_short":rs,"base_name":base}
  return idx

def build_child_to_parent(stops="stops.txt")->dict[str,str]:
  out={}
  with open(stops,encoding="utf-8-sig") as f:
    for r in csv.DictReader(f):
      sid=(r.get("stop_id") or "").strip(); pid=(r.get("parent_station") or "").strip(); lt=(r.get("location_type") or "0").strip()
      if sid and pid and is_num_parent(pid): out[sid]=pid
      if sid and lt=="1" and is_num_parent(sid): out.setdefault(sid,sid)
  vlog(f"child→parent: {len(out)}"); return out

_FEED_CACHE=None; _FEED_TS=0.0
def fetch_vp_cached():
  global _FEED_CACHE,_FEED_TS
  now=time.time()
  if _FEED_CACHE is not None and (now-_FEED_TS)<FEED_CACHE_SEC: return _FEED_CACHE,(now-_FEED_TS)
  hdr={"Authorization":f"apikey {API_TOKEN}","Accept":"application/x-protobuf","Cache-Control":"no-cache","User-Agent":"metroboard/1.0"}
  s=_sess(); time.sleep(random.uniform(0,0.25))
  merged=rt.FeedMessage(); merged.header.gtfs_realtime_version="2.0"
  total=0
  for url in URLS_VP:
    try:
      r=s.get(url,headers=hdr,timeout=20); r.raise_for_status()
      feed=rt.FeedMessage(); feed.ParseFromString(r.content)
      merged.entity.extend(feed.entity); total+=sum(1 for e in feed.entity if e.HasField("vehicle"))
      vlog(f"fetched {url}: vehicles={sum(1 for e in feed.entity if e.HasField('vehicle'))}")
    except Exception as e: vlog("WARN fetch",url,e)
  _FEED_CACHE=merged; _FEED_TS=now; vlog("feed merged vehicles",total); return merged,0.0

def collect_trains(feed,route_id_to_short:dict[str,str]):
  VP_INCOMING=rt.VehiclePosition.INCOMING_AT; VP_STOPPED=rt.VehiclePosition.STOPPED_AT
  trains=[]; by_route={}
  total=0
  for e in feed.entity:
    if not e.HasField("vehicle"): continue
    total+=1; v=e.vehicle
    rid=v.trip.route_id if v.HasField("trip") else ""
    rs=(route_id_to_short.get(rid,"") or "").upper() or None
    if ROUTE_SHORT_ALLOW and rs and rs not in ROUTE_SHORT_ALLOW: continue
    sid=getattr(v,"stop_id","")
    st=int(getattr(v,"current_status",rt.VehiclePosition.IN_TRANSIT_TO))
    lat=getattr(v.position,"latitude",None) if v.HasField("position") else None
    lon=getattr(v.position,"longitude",None) if v.HasField("position") else None
    trains.append((lat,lon,rs,sid,st))
    if rs: by_route[rs]=by_route.get(rs,0)+1
  vlog(f"collect_trains kept={len(trains)} entities={total}")
  return trains,by_route

app=FastAPI()
if CORS_ALLOW_ORIGIN:
  app.add_middleware(CORSMiddleware,allow_origins=[o.strip() for o in CORS_ALLOW_ORIGIN.split(",")],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])

ROUTE_ID_TO_SHORT={}; STATION_INDEX={}; CHILD_TO_PARENT={}
@app.on_event("startup")
def _startup():
  global ROUTE_ID_TO_SHORT,STATION_INDEX,CHILD_TO_PARENT
  ROUTE_ID_TO_SHORT=load_route_shortnames("routes.txt")
  allowed_parents=require_allowed_parents()
  allowed_pairs=require_allowed_pairs(allowed_parents_for_fallback=allowed_parents)
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
    "bbox":list(SYD_BBOX) if SYD_BBOX else None,
    "allowed_routes":sorted(ROUTE_SHORT_ALLOW) if ROUTE_SHORT_ALLOW else "all",
    "allowed_agencies":sorted(AGENCY_ALLOW) if AGENCY_ALLOW else "all",
    "cache_sec":FEED_CACHE_SEC,"feeds":URLS_VP,"verbose_logs":MB_VERBOSE,"force_distance_mode":MB_FORCE_DISTANCE,
    "distance_thresholds":{"solid":D_SOLID,"near":D_NEAR,"far":D_FAR},
    "sample_stations":list(STATION_INDEX.keys())[:10],
    "current_trains":{"total":len(trains),"by_route":by_route}
  }

@app.get("/trains")
def trains():
  feed,age=fetch_vp_cached()
  trains,by_route=collect_trains(feed,ROUTE_ID_TO_SHORT)
  def sname(st): return ["IN_TRANSIT_TO","INCOMING_AT","?","STOPPED_AT"][min(st,3)]
  out=[{"route":rs,"lat":lat,"lon":lon,"stop_id":sid,"status":sname(st),"parent_station":CHILD_TO_PARENT.get(sid,sid) if sid else None} for lat,lon,rs,sid,st in trains[:50]]
  return {"feed_age_seconds":age,"total_trains":len(trains),"sample_trains":out}

@app.get("/stations")
def stations(): return JSONResponse([STATION_INDEX[k] for k in sorted(STATION_INDEX.keys())])

@app.get("/states")
def states(only: T.Optional[str]=Query(None,description="Comma-separated station names")):
  subset=STATION_INDEX if not only else {n:STATION_INDEX[n] for n in [x.strip() for x in only.split(",") if x.strip()] if n in STATION_INDEX}
  if not subset: return JSONResponse({})
  feed,_=fetch_vp_cached()
  trains,by_route=collect_trains(feed,ROUTE_ID_TO_SHORT)
  if MB_VERBOSE: vlog(f"processing trains={len(trains)} stations={len(subset)}")
  if not trains: return JSONResponse({k:0 for k in subset.keys()})
# group by route, keep None for unknown routes (distance-only bucket)
grouped = {}
for t in trains:
    grouped.setdefault(t[2], []).append(t)  # key is route_short or None

VP_INCOMING = rt.VehiclePosition.INCOMING_AT
VP_STOPPED  = rt.VehiclePosition.STOPPED_AT

out = {}
matches = 0
dist_fb = 0

for name, meta in subset.items():
    pid = meta["stop_id"]
    slat, slon = meta["lat"], meta["lon"]
    rs = (meta.get("route_short") or "").upper()

    best = 0
    best_d = None

    # 1) Consider only trains on the SAME ROUTE for direct parent matches
    for (tlat, tlon, trs, child_sid, st) in grouped.get(rs, []):
        if not MB_FORCE_DISTANCE and child_sid:
            parent = CHILD_TO_PARENT.get(child_sid) or (child_sid if is_num_parent(child_sid) else None)
            if parent == pid:
                if st == VP_STOPPED:
                    best = 3
                    matches += 1
                    break
                elif st == VP_INCOMING:
                    best = max(best, 2)
                    matches += 1
        # Also track nearest distance for same-route trains (as a tie-breaker)
        if best < 3 and tlat is not None and tlon is not None:
            d = hav_m(slat, slon, tlat, tlon)
            if best_d is None or d < best_d:
                best_d = d

    # 2) For UNKNOWN-ROUTE trains, **distance only** (no direct parent match!)
    if best < 3:
        for (tlat, tlon, trs, child_sid, st) in grouped.get(None, []):
            if tlat is None or tlon is None:
                continue
            d = hav_m(slat, slon, tlat, tlon)
            if best_d is None or d < best_d:
                best_d = d

    # 3) Apply distance-based fallback thresholds if we didn’t get a direct match
    if best == 0 and best_d is not None:
        best = status_from_dist(best_d)
        if best > 0:
            dist_fb += 1

    out[name] = best
  if MB_VERBOSE:
    act=sum(1 for v in out.values() if v>0)
    vlog(f"result active={act}/{len(subset)} matches={matches} distance_fb={dist_fb}")
  return JSONResponse(out)
