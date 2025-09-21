# app.py — Metroboard backend (Rail + Metro) with OOM-safe GTFS merge
import os, time, csv, json, re, typing as T, random, requests, io, zipfile, shutil, tempfile, threading, gc
from pathlib import Path
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.transit import gtfs_realtime_pb2 as rt
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

"""
ENV TOGGLES (optional):
- MB_SKIP_PRIVATE=1              -> ignore the private GitHub API asset
- MB_ONLY_FIRST_SOURCE=1         -> after the first usable GTFS source, skip the rest
- MB_MAX_STOP_TIMES=<int>        -> cap rows written to stop_times.txt (debug/testing)
- MB_STOP_TIMES_STRATEGY=first|append|dedup
    first  = copy stop_times from the first source that has it (no dedup)  [default]
    append = append stop_times from all sources (no dedup)
    dedup  = dedup stop_times across sources (HIGH memory — not recommended)
"""

# ---------------- Env helpers ----------------
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

_PARENTS_ID_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
def is_num_parent(s: str) -> bool:
    return bool(s and _PARENTS_ID_RE.match(s))

API_TOKEN = os.getenv("TFNSW_API_KEY") or os.getenv("TFNSW_API_TOKEN")
if not API_TOKEN:
    raise RuntimeError("Missing TFNSW_API_KEY")

URLS_VP = [
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/sydneytrains",
    "https://api.transport.nsw.gov.au/v2/gtfs/vehiclepos/metro",
]

GTFS_DIR = os.getenv("GTFS_DIR", "./gtfs")

# GitHub source (for your "full trains" pack)
GH_OWNER = os.getenv("GTFS_GH_OWNER", "")
GH_REPO  = os.getenv("GTFS_GH_REPO", "")
GH_TAG   = os.getenv("GTFS_GH_TAG", "")
GH_ASSET = os.getenv("GTFS_GH_ASSET", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# Direct/public zips (e.g. Metro pseudo)
GTFS_STATIC_ZIP_URL  = os.getenv("GTFS_STATIC_ZIP_URL", "")
GTFS_STATIC_ZIP_URLS = [u.strip() for u in os.getenv("GTFS_STATIC_ZIP_URLS", "").split(",") if u.strip()]
if GTFS_STATIC_ZIP_URL and GTFS_STATIC_ZIP_URL not in GTFS_STATIC_ZIP_URLS:
    GTFS_STATIC_ZIP_URLS.append(GTFS_STATIC_ZIP_URL)

# Optional toggles
MB_SKIP_PRIVATE       = os.getenv("MB_SKIP_PRIVATE", "0") in ("1", "true", "yes")
MB_ONLY_FIRST_SOURCE  = os.getenv("MB_ONLY_FIRST_SOURCE", "0") in ("1", "true", "yes")
MB_MAX_STOP_TIMES_RAW = os.getenv("MB_MAX_STOP_TIMES", "").strip()
MB_MAX_STOP_TIMES     = int(MB_MAX_STOP_TIMES_RAW) if MB_MAX_STOP_TIMES_RAW.isdigit() else None
MB_STOP_TIMES_STRATEGY = (os.getenv("MB_STOP_TIMES_STRATEGY", "first") or "first").lower()

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
MB_PERMISSIVE_PAIRS = os.getenv("MB_PERMISSIVE_PAIRS", "1") != "0"

def vlog(*a):
    if MB_VERBOSE: print("[metroboard]", *a)

# ---------------- Utils ----------------
EXCLUDE_SUB = ("wharf","light rail","interchange","motorway"," m2","b-line")
SHORT_RE = re.compile(r'\b([TM]\d{1,2})\b', re.I)

def _sess():
    s=requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=4, backoff_factor=0.6,
        status_forcelist=(500,502,503,504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False)))
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

# ---------------- GTFS merge helpers ----------------
NEEDED = ["routes.txt","trips.txt","stops.txt","stop_times.txt","agency.txt"]

def _promote_inner_gtfs_files(root: Path):
    needed = set(NEEDED)
    if all((root / f).exists() for f in needed):
        return
    for cand in sorted([p for p in root.rglob("*") if p.is_dir()], key=lambda p: len(p.parts)):
        if all((cand / f).exists() for f in needed):
            for f in needed:
                src = cand / f
                dst = root / f
                if not dst.exists():
                    shutil.move(str(src), str(dst))
            return

def _extract_zip_bytes(data: bytes, out_dir: Path):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(out_dir)
    _promote_inner_gtfs_files(out_dir)

def _download_release_asset_via_github(owner, repo, tag, asset_name, token) -> bytes:
    if not token:
        raise RuntimeError("GITHUB_TOKEN required for GitHub release download")
    api_base = f"https://api.github.com/repos/{owner}/{repo}"
    headers_json = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    rel = requests.get(f"{api_base}/releases/tags/{tag}", headers=headers_json, timeout=60)
    rel.raise_for_status()
    data = rel.json()
    asset = next((a for a in data.get("assets", []) if a.get("name") == asset_name), None)
    if not asset:
        raise RuntimeError(f"Asset '{asset_name}' not found on release tag '{tag}'")
    asset_id = asset["id"]
    headers_bin = {"Authorization": f"token {token}", "Accept": "application/octet-stream"}
    dl = requests.get(f"{api_base}/releases/assets/{asset_id}", headers=headers_bin, timeout=300)
    dl.raise_for_status()
    return dl.content

def _download_sources_to_tempdirs() -> list[Path]:
    temp_dirs: list[Path] = []

    # Private/full pack via GitHub API
    if not MB_SKIP_PRIVATE and GH_OWNER and GH_REPO and GH_TAG and GH_ASSET:
        vlog(f"fetching GTFS via GitHub API {GH_OWNER}/{GH_REPO}@{GH_TAG} asset={GH_ASSET}")
        try:
            data = _download_release_asset_via_github(GH_OWNER, GH_REPO, GH_TAG, GH_ASSET, GITHUB_TOKEN)
            td = Path(tempfile.mkdtemp(prefix="gtfs_src_"))
            _extract_zip_bytes(data, td)
            temp_dirs.append(td)
            if MB_ONLY_FIRST_SOURCE:
                return temp_dirs
        except Exception as e:
            vlog("WARN GitHub API asset fetch failed:", e)

    # Public/direct zips — skip cleanly on 404/403/etc.
    seen_urls: set[str] = set()
    for url in GTFS_STATIC_ZIP_URLS:
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        headers = {}
        if "github.com" in url.lower() and GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
            headers["Accept"] = "application/octet-stream"

        vlog(f"downloading GTFS static zip: {url}")
        try:
            r = requests.get(url, headers=headers, timeout=300)
            if r.status_code in (403, 404):
                vlog(f"skip static GTFS (HTTP {r.status_code}): {url}")
                continue
            r.raise_for_status()
            td = Path(tempfile.mkdtemp(prefix="gtfs_src_"))
            _extract_zip_bytes(r.content, td)
            temp_dirs.append(td)
            if MB_ONLY_FIRST_SOURCE and temp_dirs:
                return temp_dirs
        except Exception as e:
            vlog("WARN static GTFS download failed:", url, e)

    if not temp_dirs:
        raise RuntimeError("GTFS not present and no usable download source")
    return temp_dirs

def _merge_stop_times(sources: list[Path], dest_dir: Path):
    """OOM-safe: default 'first' strategy writes stop_times from the first source only (no dedup)."""
    strategy = MB_STOP_TIMES_STRATEGY
    out_path = dest_dir / "stop_times.txt"
    tmp_path = dest_dir / "stop_times.txt.tmp"

    def _copy_from(p: Path, writer: csv.DictWriter, wrote_header: bool, rows_written: int) -> tuple[bool,int]:
        with p.open(encoding="utf-8-sig") as f:
            r = csv.DictReader(f)
            if not wrote_header:
                writer.writeheader()
                wrote_header = True
            for row in r:
                if MB_MAX_STOP_TIMES and rows_written >= MB_MAX_STOP_TIMES:
                    break
                writer.writerow({h: row.get(h, "") for h in writer.fieldnames})
                rows_written += 1
        return wrote_header, rows_written

    # find first file for header
    first_file = None
    for src in sources:
        p = src / "stop_times.txt"
        if p.exists():
            first_file = p
            break
    if not first_file:
        raise RuntimeError("No stop_times.txt found in any source")

    # open header to get fieldnames
    with first_file.open(encoding="utf-8-sig") as f0:
        r0 = csv.DictReader(f0)
        header = r0.fieldnames or []
    if not header:
        raise RuntimeError("stop_times.txt has no header")

    rows_written = 0
    wrote_header = False
    tmp_f = tmp_path.open("w", newline="", encoding="utf-8")
    writer = csv.DictWriter(tmp_f, fieldnames=header)

    try:
        if strategy == "first":
            wrote_header, rows_written = _copy_from(first_file, writer, wrote_header, rows_written)
        elif strategy == "append":
            for src in sources:
                p = src / "stop_times.txt"
                if not p.exists():
                    continue
                wrote_header, rows_written = _copy_from(p, writer, wrote_header, rows_written)
                if MB_MAX_STOP_TIMES and rows_written >= MB_MAX_STOP_TIMES:
                    break
        elif strategy == "dedup":
            # WARNING: memory-heavy; keep tiny LRU window only to avoid total explosion
            seen = set()
            writer.writeheader(); wrote_header=True
            for src in sources:
                p = src / "stop_times.txt"
                if not p.exists():
                    continue
                with p.open(encoding="utf-8-sig") as f:
                    r = csv.DictReader(f)
                    for row in r:
                        if MB_MAX_STOP_TIMES and rows_written >= MB_MAX_STOP_TIMES:
                            break
                        key = (row.get("trip_id","").strip(), row.get("stop_id","").strip(), row.get("stop_sequence","").strip())
                        if key in seen:
                            continue
                        # keep set from growing without bound (sacrifice perfect dedup)
                        if len(seen) > 500000:
                            seen.clear()
                            gc.collect()
                        seen.add(key)
                        writer.writerow({h: row.get(h, "") for h in header})
                        rows_written += 1
        else:
            # fallback -> first
            wrote_header, rows_written = _copy_from(first_file, writer, wrote_header, rows_written)
    finally:
        tmp_f.close()

    tmp_path.replace(out_path)
    vlog(f"merged stop_times.txt ({strategy}): rows={rows_written}")

def _merge_csvs_streaming(sources: list[Path], dest_dir: Path):
    """
    Streaming, memory-safe merge:
      - For all files EXCEPT stop_times.txt -> dedup across sources using a small set
      - For stop_times.txt                   -> OOM-safe strategy (default 'first', no dedup)
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    # First handle normal files (no giant sets)
    keys = {
        "routes.txt":      ["route_id"],
        "trips.txt":       ["trip_id"],
        "stops.txt":       ["stop_id"],
        "agency.txt":      ["agency_id"],
    }

    for fname in ["routes.txt","trips.txt","stops.txt","agency.txt"]:
        header: list[str] = []
        seen: set[tuple] = set()
        out_tmp = dest_dir / (fname + ".tmp")
        out_final = dest_dir / fname

        out_f = None
        writer = None
        written_rows = 0

        try:
            for src in sources:
                p = src / fname
                if not p.exists():
                    continue
                with p.open(encoding="utf-8-sig") as f:
                    r = csv.DictReader(f)
                    if not header:
                        header = r.fieldnames or []
                        out_f = out_tmp.open("w", newline="", encoding="utf-8")
                        writer = csv.DictWriter(out_f, fieldnames=header)
                        writer.writeheader()

                    for row in r:
                        pk_names = keys[fname]
                        if fname == "agency.txt" and not (row.get("agency_id") or "").strip():
                            row["agency_id"] = (row.get("agency_name") or "").strip() or f"AGENCY_{len(seen)+1}"
                        key = tuple((row.get(k) or "").strip() for k in pk_names)
                        if key in seen:
                            continue
                        seen.add(key)

                        for h in header:
                            row.setdefault(h, "")
                        writer.writerow(row)
                        written_rows += 1

                gc.collect()

            if not header:
                raise RuntimeError(f"No header for {fname} after merge")
        finally:
            if out_f:
                out_f.close()

        if out_tmp.exists():
            out_tmp.replace(out_final)
        vlog(f"merged {fname}: rows={written_rows}")

    # Then the big boy: stop_times (special handling)
    _merge_stop_times(sources, dest_dir)

    # quick counts
    def _count_rows(path: Path) -> int:
        try:
            with path.open(encoding="utf-8") as f:
                return max(0, sum(1 for _ in f) - 1)
        except Exception:
            return 0
    vlog(
        f"Merged GTFS: routes={_count_rows(dest_dir/'routes.txt')}, "
        f"trips={_count_rows(dest_dir/'trips.txt')}, "
        f"stops={_count_rows(dest_dir/'stops.txt')}"
    )
    gc.collect()

def ensure_gtfs(dirpath: str):
    root = Path(dirpath)
    root.mkdir(parents=True, exist_ok=True)
    if all((root / f).exists() for f in NEEDED):
        vlog(f"GTFS already present at {root}")
        return
    src_dirs = _download_sources_to_tempdirs()
    vlog(f"Fetched {len(src_dirs)} GTFS source(s); merging into {root}")
    _merge_csvs_streaming(src_dirs, root)

# ---------------- Route/GTFS helpers ----------------
def load_route_overrides(path:str,json_env:str)->dict[str,str]:
    out={}
    try:
        p = Path(path)
        if p.exists():
            with p.open(encoding="utf-8-sig") as f:
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

def load_route_shortnames(routes_path: str)->dict[str,str]:
    ov=load_route_overrides(ROUTE_SHORT_OVERRIDES_PATH,ROUTE_SHORT_OVERRIDES_JSON); out={}
    with open(routes_path,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid=(r.get("route_id") or "").strip()
            if not rid: continue
            rs=ov.get(rid) or derive_short(r.get("route_short_name"),r.get("route_long_name"))
            if rs: out[rid]=rs
    vlog(f"route_short: {len(out)} (ov={len(ov)})"); return out

def require_allowed_parents(stops: str,routes: str,trips: str,stop_times: str,agency: str,types: set[int]):
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
        row=stops_map.get(sid)
        if not row: return None
        p=(row.get("parent_station") or "").strip()
        return p or (sid if sid in parents else None)

    # 1) parents proven by stop_times (trips)
    allowed=set()
    with open(stop_times,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid=trip_route.get((r.get("trip_id") or "").strip(),"")
            rtype,ag=route_meta.get(rid,(None,""))
            if rtype is None or rtype not in types: continue
            if AGENCY_ALLOW and ag not in AGENCY_ALLOW: continue
            p=parent_of((r.get("stop_id") or "").strip())
            if p and is_num_parent(p): allowed.add(p)

    # 2) name-based parents
    named=set()
    with open(stops,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("location_type") or "0").strip()!="1": continue
            pid=(r.get("stop_id") or "").strip()
            if not is_num_parent(pid): continue
            n=(r.get("stop_name") or "")
            n_low=n.lower().strip()
            if any(x in n_low for x in EXCLUDE_SUB): continue
            if n_low.endswith(" station") or n_low.endswith(" railway station"):
                named.add(pid)
    union=allowed|named
    if MB_VERBOSE:
        vlog(f"parents by trips+names: {len(union)} (trips={len(allowed)}, names+={len(union)-len(allowed)})")
    return union

def require_allowed_pairs(stops: str,routes: str,trips: str,stop_times: str,agency: str,types: set[int],allowed_parents_for_fallback=None):
    ag_name={}
    with open(agency,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f): ag_name[(r.get("agency_id") or "").strip()]=(r.get("agency_name") or "").strip()
    rt_meta={}
    with open(routes,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid=(r.get("route_id") or "").strip()
            try: rtype=int((r.get("route_type") or "3").strip() or "3")
            except: rtype=3
            rt_meta[rid]=(rtype,ag_name.get((r.get("agency_id") or "").strip(),""))
    trip_route={}
    with open(trips,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f): trip_route[(r.get("trip_id") or "").strip()]=(r.get("route_id") or "").strip()
    stops_map,parents={},set()
    with open(stops,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid=(r.get("stop_id") or "").strip(); stops_map[sid]=r
            if (r.get("location_type") or "0").strip()=="1": parents.add(sid)
    def parent_of(sid):
        row=stops_map.get(sid)
        if not row: return None
        p=(row.get("parent_station") or "").strip()
        return p or (sid if sid in parents else None)

    rshort=load_route_shortnames(routes)
    pairs=set()
    with open(stop_times,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rid=trip_route.get((r.get("trip_id") or "").strip(),"")
            rtype,ag=rt_meta.get(rid,(None,""))
            if rtype is None or rtype not in types: continue
            if AGENCY_ALLOW and ag not in AGENCY_ALLOW: continue
            rs=(rshort.get(rid,"") or "").upper()
            if not rs: continue
            if ROUTE_SHORT_ALLOW and rs not in ROUTE_SHORT_ALLOW: continue
            p=parent_of((r.get("stop_id") or "").strip())
            if p and is_num_parent(p): pairs.add((p,rs))

    # permissive fallback (generate station×line for known T*/M* shortnames)
    if not pairs and MB_PERMISSIVE_PAIRS:
        known={ (rs or "").upper().strip()
                for rs in rshort.values()
                if re.fullmatch(r"[TM]\d{1,2}", (rs or "").upper().strip()) }
        for rs_env in ROUTE_SHORT_ALLOW:
            if re.fullmatch(r"[TM]\d{1,2}", rs_env):
                known.add(rs_env)
        if ROUTE_SHORT_ALLOW:
            known &= {x.upper() for x in ROUTE_SHORT_ALLOW}
        if known:
            if not allowed_parents_for_fallback:
                allowed_parents_for_fallback={sid for sid,row in stops_map.items()
                                              if (row.get("location_type") or "0").strip()=="1" and is_num_parent(sid)}
            pairs={(pid,rs) for pid in allowed_parents_for_fallback for rs in known}
    return pairs

def route_suffix(rs:str)->str:
    rs=(rs or "").upper()
    if rs.startswith("T"): return rs[1:]
    if rs.startswith("M"): return "M"
    return rs or "?"

def load_station_index(stops: str, allowed_parents: set[str], allowed_pairs: set[tuple[str,str]]):
    parents={}
    with open(stops,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            lt=(r.get("location_type") or "0").strip(); sid=(r.get("stop_id") or "").strip()
            name=(r.get("stop_name") or "").strip(); lat_s,lon_s=r.get("stop_lat"),r.get("stop_lon")
            lat=float(lat_s) if lat_s else None; lon=float(lon_s) if lon_s else None
            if lt=="1" and sid in allowed_parents and not any(x in name.lower() for x in EXCLUDE_SUB):
                parents[sid]={"name":name,"lat":lat,"lon":lon}
    by_parent={}
    for pid,rs in allowed_pairs: by_parent.setdefault(pid,set()).add(rs)
    idx={}
    for pid,meta in parents.items():
        base=clean_name(meta["name"]); lat,lon=meta["lat"],meta["lon"]
        for rs in sorted(by_parent.get(pid,set())):
            for rs_eff in LINE_ALIASES.get(rs,[rs]):  # expand aliasing
                suf=route_suffix(rs_eff); key=f"{base}{suf}"
                if key in idx: key=f"{base}{suf} ({pid})"
                idx[key]={"stop_id":pid,"name":key,"lat":lat,"lon":lon,
                          "route_short":rs_eff,"base_name":base}
    return idx

def build_child_to_parent(stops: str):
    out={}
    with open(stops,encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid=(r.get("stop_id") or "").strip()
            pid=(r.get("parent_station") or "").strip()
            lt=(r.get("location_type") or "0").strip()
            if sid and pid:
                out[sid] = pid
            if sid and lt == "1":
                out.setdefault(sid, sid)
    return out

# ---------------- Feed ----------------
_FEED_CACHE=None; _FEED_TS=0.0
def fetch_vp_cached():
    global _FEED_CACHE,_FEED_TS
    now=time.time()
    if _FEED_CACHE is not None and (now-_FEED_TS)<FEED_CACHE_SEC: return _FEED_CACHE,(now-_FEED_TS)
    hdr={"Authorization":f"apikey {API_TOKEN}","Accept":"application/x-protobuf",
         "Cache-Control":"no-cache","User-Agent":"metroboard/1.0"}
    s=_sess(); time.sleep(random.uniform(0,0.25))
    merged=rt.FeedMessage(); merged.header.gtfs_realtime_version="2.0"
    for url in URLS_VP:
        try:
            r=s.get(url,headers=hdr,timeout=20); r.raise_for_status()
            feed=rt.FeedMessage(); feed.ParseFromString(r.content)
            merged.entity.extend(feed.entity)
            if MB_VERBOSE:
                kept=sum(1 for e in feed.entity if e.HasField("vehicle"))
                vlog(f"fetched {url}: vehicles={kept}")
        except Exception as e: vlog("WARN fetch",url,e)
    _FEED_CACHE=merged; _FEED_TS=now; return merged,0.0

# ---- Metro-friendly short-name inference ----
_TM_RE = re.compile(r'\b([TM]\d{1,2})\b', re.I)
def _infer_short_from_ids(route_id:str, trip_id:str)->T.Optional[str]:
    for text in (route_id or "", trip_id or ""):
        m=_TM_RE.search(text)
        if m: return m.group(1).upper()
    s=(route_id or "")+" "+(trip_id or "")
    if "M1" in s.upper(): return "M1"
    return None

def collect_trains(feed,route_id_to_short:dict[str,str]):
    trains=[]; by_route={}
    for e in feed.entity:
        if not e.HasField("vehicle"): continue
        v=e.vehicle
        rid=v.trip.route_id if v.HasField("trip") else ""
        tid=v.trip.trip_id if v.HasField("trip") else ""
        rs=(route_id_to_short.get(rid,"") or "").upper() or None
        if rs is None: rs=_infer_short_from_ids(rid,tid)
        if ROUTE_SHORT_ALLOW and rs and rs not in ROUTE_SHORT_ALLOW: continue
        sid=getattr(v,"stop_id",""); st=int(getattr(v,"current_status",rt.VehiclePosition.IN_TRANSIT_TO))
        lat=getattr(v.position,"latitude",None) if v.HasField("position") else None
        lon=getattr(v.position,"longitude",None) if v.HasField("position") else None
        trains.append((lat,lon,rs,sid,st))
        if rs: by_route[rs]=by_route.get(rs,0)+1
    if MB_VERBOSE: vlog(f"collect_trains kept={len(trains)} by_route={by_route}")
    return trains,by_route

# ---------------- API ----------------
app=FastAPI()
if CORS_ALLOW_ORIGIN:
    app.add_middleware(CORSMiddleware,allow_origins=[o.strip() for o in CORS_ALLOW_ORIGIN.split(",")],
                       allow_credentials=True,allow_methods=["*"],allow_headers=["*"])

# Async init flags
ROUTE_ID_TO_SHORT: dict[str,str]={}
STATION_INDEX: dict[str,dict]={}
CHILD_TO_PARENT: dict[str,str]={}
INIT_DONE=False; INIT_ERR: T.Optional[str]=None

def _init_gtfs_and_index():
    global ROUTE_ID_TO_SHORT, STATION_INDEX, CHILD_TO_PARENT, INIT_DONE, INIT_ERR
    try:
        ensure_gtfs(GTFS_DIR)
        routes_p     = str(Path(GTFS_DIR)/"routes.txt")
        trips_p      = str(Path(GTFS_DIR)/"trips.txt")
        stops_p      = str(Path(GTFS_DIR)/"stops.txt")
        stop_times_p = str(Path(GTFS_DIR)/"stop_times.txt")
        agency_p     = str(Path(GTFS_DIR)/"agency.txt")

        ROUTE_ID_TO_SHORT=load_route_shortnames(routes_p)
        allowed_parents=require_allowed_parents(stops_p,routes_p,trips_p,stop_times_p,agency_p,ROUTE_TYPES_ALLOW)
        allowed_pairs=require_allowed_pairs(stops_p,routes_p,trips_p,stop_times_p,agency_p,ROUTE_TYPES_ALLOW,
                                            allowed_parents_for_fallback=allowed_parents)
        STATION_INDEX=load_station_index(stops_p,allowed_parents,allowed_pairs)
        CHILD_TO_PARENT=build_child_to_parent(stops_p)
        if not STATION_INDEX: raise RuntimeError("No stations after filtering")
        vlog(f"GTFS ready: parents={len(allowed_parents)} stations={len(STATION_INDEX)}")
        INIT_DONE=True; INIT_ERR=None
    except Exception as e:
        INIT_ERR=str(e); INIT_DONE=False; vlog("ERROR initialization:",e)

@app.on_event("startup")
def _startup():
    t=threading.Thread(target=_init_gtfs_and_index,daemon=True); t.start()

@app.get("/")
def root(): return {"ok":True,"endpoints":["/states","/stations","/healthz","/debug","/trains"]}

@app.get("/healthz")
def healthz():
    age=None if _FEED_TS==0 else (time.time()-_FEED_TS)
    status="ok" if INIT_DONE else ("error" if INIT_ERR else "initializing")
    return {"status":status,"error":INIT_ERR,"stations":len(STATION_INDEX),
            "trains_last_fetch_s":age,"gtfs_dir":GTFS_DIR}

@app.get("/debug")
def debug():
    feed,_=fetch_vp_cached(); trains,by_route=collect_trains(feed,ROUTE_ID_TO_SHORT)
    return {"stations_count":len(STATION_INDEX),
            "allowed_routes":sorted(ROUTE_SHORT_ALLOW) if ROUTE_SHORT_ALLOW else "all",
            "allowed_agencies":sorted(AGENCY_ALLOW) if AGENCY_ALLOW else "all",
            "cache_sec":FEED_CACHE_SEC,"feeds":URLS_VP,
            "gtfs_dir":GTFS_DIR,
            "current_trains":{"total":len(trains),"by_route":by_route},
            "init_done": INIT_DONE, "init_error": INIT_ERR}

@app.get("/trains")
def trains():
    if not INIT_DONE:
        return {"status":"initializing","total_trains":0,"sample_trains":[],"feed_age_seconds":None}
    feed,age=fetch_vp_cached()
    trains,by_route=collect_trains(feed,ROUTE_ID_TO_SHORT)
    out=[{"route":rs,"lat":lat,"lon":lon,"stop_id":sid,"status":st,
          "parent_station":CHILD_TO_PARENT.get(sid,sid) if sid else None}
         for lat,lon,rs,sid,st in trains[:50]]
    return {"feed_age_seconds":age,"total_trains":len(trains),"sample_trains":out}

@app.get("/stations")
def stations():
    if not INIT_DONE:
        return JSONResponse({"status":"initializing","stations":0})
    return JSONResponse([STATION_INDEX[k] for k in sorted(STATION_INDEX.keys())])

@app.get("/states")
def states(only: T.Optional[str]=Query(None)):
    if not INIT_DONE:
        return JSONResponse({})
    subset = STATION_INDEX if not only else {
        n: STATION_INDEX[n] for n in [x.strip() for x in only.split(",") if x.strip()] if n in STATION_INDEX
    }
    if not subset: return JSONResponse({})

    feed,_ = fetch_vp_cached()
    trains,by_route = collect_trains(feed,ROUTE_ID_TO_SHORT)
    if MB_VERBOSE: vlog(f"processing trains={len(trains)} stations={len(subset)}")
    if not trains: return JSONResponse({k:0 for k in subset.keys()})

    grouped: dict[T.Optional[str], list[tuple]] = {}
    for t in trains:
        grouped.setdefault(t[2], []).append(t)

    VP_INCOMING = rt.VehiclePosition.INCOMING_AT
    VP_STOPPED  = rt.VehiclePosition.STOPPED_AT

    out={}; matches=0; dist_fb=0
    for name,meta in subset.items():
        pid=meta["stop_id"]; slat,slon=meta["lat"],meta["lon"]; rs=(meta.get("route_short") or "").upper()
        rs_list=[rs] if rs else []
        if not STRICT_LINE_MATCH and rs:
            rs_list = LINE_ALIASES.get(rs, [rs])

        best=0; best_d=None
        cand_trains=[]
        for rsi in rs_list:
            cand_trains.extend(grouped.get(rsi, []))
        cand_trains.extend(grouped.get(None, []))  # distance-only bucket

        for (tlat,tlon,trs,child_sid,st) in cand_trains:
            if not MB_FORCE_DISTANCE and child_sid:
                parent = CHILD_TO_PARENT.get(child_sid) or (child_sid if is_num_parent(child_sid) else None)
                if parent == pid:
                    if st == VP_STOPPED: best=3; matches+=1; break
                    elif st == VP_INCOMING: best=max(best,2); matches+=1
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
