#!/usr/bin/env python3
"""
SlopesDB pipeline — fetch, process, and export ski run slope data for all configured resorts.

For each resort:
  - Downloads run geometry from OpenStreetMap (Overpass API)
  - Downloads a DEM (USGS 3DEP 2m for US resorts; Copernicus GLO-30 30m for Canadian resorts)
  - Stitches same-name OSM ways with touching endpoints into single runs
  - Computes slope profiles at multiple smoothing levels (2m / 10m / 30m)
  - Computes face steepness (Horn gradient, direction-filtered) and line steepness
  - Exports per-resort JSON for the web UI and a static summary chart

Requirements:
    pip install requests numpy matplotlib rasterio
"""

import os
import math
import time
import requests
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.gridspec import GridSpec, GridSpecFromSubplotSpec
import rasterio
from math import radians, cos, sin, asin, sqrt, atan2, degrees

# ── Config ───────────────────────────────────────────────────────────────────

RESORTS = [
    {
        "name": "Palisades Tahoe",
        "osm_bbox": "(39.15,-120.30,39.27,-120.17)",
        "dem_bbox": (-120.30, 39.15, -120.17, 39.27),  # west,south,east,north
        "color": "steelblue",
        "dem_resolution_m": 2,
    },
    {
        "name": "Northstar",
        "osm_bbox": "(39.23,-120.16,39.30,-120.09)",
        "dem_bbox": (-120.16, 39.23, -120.09, 39.30),
        "color": "forestgreen",
        "dem_resolution_m": 2,
    },
    {
        "name": "Sugar Bowl",
        "osm_bbox": "(39.28,-120.38,39.33,-120.32)",
        "dem_bbox": (-120.38, 39.28, -120.32, 39.33),
        "color": "darkorange",
        "dem_resolution_m": 2,
    },
    {
        "name": "Mount Norquay",
        "osm_bbox": "(51.19,-115.63,51.23,-115.56)",
        "dem_bbox": (-115.63, 51.19, -115.56, 51.23),
        "color": "royalblue",
        "dem_source": "copernicus",
        "dem_resolution_m": 30,
        "spotlio_uuid": "54e0b321fbb08c7c6a51abd24bb5ea158d5c3eb479a189662507fab8e5238836",
    },
    {
        "name": "Sunshine Village",
        "osm_bbox": "(51.05,-115.82,51.12,-115.73)",
        "dem_bbox": (-115.82, 51.05, -115.73, 51.12),
        "color": "goldenrod",
        "dem_source": "copernicus",
        "dem_resolution_m": 30,
        "spotlio_uuid": "c15dc51e0e08ee96c8e192afb2b7c04b073c3d37682dd7d8f8bd319fd76221d5",
    },
    {
        "name": "Lake Louise",
        "osm_bbox": "(51.40,-116.22,51.47,-116.09)",
        "dem_bbox": (-116.22, 51.40, -116.09, 51.47),
        "color": "mediumorchid",
        "dem_source": "copernicus",
        "dem_resolution_m": 30,
        "spotlio_uuid": "1cfc07ddc36438c51a0d0a9c9a4c7fe92f6558c8b2094e35d8c4d1c250e6d2a1",
    },
    {
        "name": "Whistler Blackcomb",
        "osm_bbox": "(50.04,-123.00,50.15,-122.85)",
        "dem_bbox": (-123.00, 50.04, -122.85, 50.15),
        "color": "teal",
        "dem_source": "copernicus",
        "dem_resolution_m": 30,
    },
]

SAMPLE_SPACING_M = 2        # meters between sample points along each run
STEEPEST_WINDOW_M = 10     # rolling-mean window for steepest-pitch metric (metres)
SMOOTH_POINTS    = 30       # default smooth level (m); used for the static PNG only
SMOOTH_LEVELS    = [2, 10, 30]   # exported for the web UI (smoothing window in meters)
GEO_SEGMENT_STEP          = 15   # take every Nth 2m point → ~30m map segments
TRAVERSE_DELTA_THRESHOLD  = 5.0  # flag as potential traverse if face_delta >= this (degrees)
FACE_DISPLAY_CAP          = 8.0  # geo display: face can exceed line by at most this (degrees)
CACHE_DIR        = "cache"  # local cache for all downloaded data
OVERPASS_URLS    = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
USGS_WCS_URL     = (
    "https://elevation.nationalmap.gov/arcgis/services/"
    "3DEPElevation/ImageServer/WCSServer"
)
COPERNICUS_S3    = "https://copernicus-dem-30m.s3.amazonaws.com"
SPOTLIO_BASE     = "https://autogen.3dmap.spotlio.com"
MIN_SPOTLIO_PTS  = 5    # discard Spotlio runs with fewer than this many coordinate points

# ── Cache helpers ─────────────────────────────────────────────────────────────

import json

def _cache_path(name: str, ext: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{name.replace(' ', '_')}.{ext}")


def load_json_cache(name: str) -> list | None:
    path = _cache_path(name, "json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def save_json_cache(name: str, data) -> None:
    path = _cache_path(name, "json")
    with open(path, "w") as f:
        json.dump(data, f)


def profiles_cache_path(name: str, smooth_pts: int) -> str:
    return _cache_path(f"{name}_profiles_s{smooth_pts}", "json")


def load_profiles(name: str, smooth_pts: int) -> list | None:
    path = profiles_cache_path(name, smooth_pts)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        raw = json.load(f)
    return [
        (r["name"],
         np.array(r["dist_m"]) if r["dist_m"] is not None else None,
         np.array(r["slope_deg"]) if r["slope_deg"] is not None else None,
         r.get("steepest"),
         r.get("face_steepest"))     # None if not yet computed
        for r in raw
    ]


def save_profiles(name: str, smooth_pts: int, results: list) -> None:
    path = profiles_cache_path(name, smooth_pts)
    serialisable = [
        {"name": run_name,
         "dist_m":        dist.tolist() if dist is not None else None,
         "slope_deg":     slope.tolist() if slope is not None else None,
         "steepest":      steepest_override,
         "face_steepest": face_steep}
        for run_name, dist, slope, steepest_override, face_steep in results
    ]
    with open(path, "w") as f:
        json.dump(serialisable, f)
    print(f"  Profiles saved → {path}")


# ── DEM download ──────────────────────────────────────────────────────────────

def dem_path_for(name: str, resolution_m: int) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return _cache_path(f"{name}_{resolution_m}m", "tif")


WCS_MAX_PX  = 2000   # safe per-dimension limit for USGS 3DEP WCS
WCS_RETRIES = 3      # retry count for transient tile errors


def _download_dem_tile(bbox_wsen: tuple, output_path: str, resolution_m: int) -> None:
    """Download a single WCS tile, retrying up to WCS_RETRIES times on error."""
    west, south, east, north = bbox_wsen
    lat_c = (south + north) / 2
    width_m  = (east - west) * 111_000 * math.cos(math.radians(lat_c))
    height_m = (north - south) * 111_000
    width_px  = max(10, int(width_m  / resolution_m))
    height_px = max(10, int(height_m / resolution_m))
    params = {
        "SERVICE":  "WCS",
        "VERSION":  "1.0.0",
        "REQUEST":  "GetCoverage",
        "COVERAGE": "DEP3Elevation",
        "CRS":      "EPSG:4326",
        "BBOX":     f"{west},{south},{east},{north}",
        "WIDTH":    width_px,
        "HEIGHT":   height_px,
        "FORMAT":   "GeoTIFF",
    }
    last_err = None
    for attempt in range(WCS_RETRIES):
        try:
            resp = requests.get(USGS_WCS_URL, params=params, timeout=600, stream=True)
            resp.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
            return
        except Exception as e:
            last_err = e
            if attempt < WCS_RETRIES - 1:
                time.sleep(5)
    raise last_err


def download_dem(bbox_wsen: tuple, output_path: str, resolution_m: int = 10):
    """
    Download a USGS 3DEP GeoTIFF via WCS for the given bounding box.
    Tiles requests automatically when the bbox exceeds WCS_MAX_PX per dimension.
    bbox_wsen: (west, south, east, north) in WGS-84 degrees.
    """
    import tempfile
    from rasterio.merge import merge as rio_merge
    from rasterio.transform import from_bounds

    west, south, east, north = bbox_wsen
    lat_c = (south + north) / 2
    width_m  = (east - west) * 111_000 * math.cos(math.radians(lat_c))
    height_m = (north - south) * 111_000
    total_w_px = max(100, int(width_m  / resolution_m))
    total_h_px = max(100, int(height_m / resolution_m))

    n_cols = math.ceil(total_w_px / WCS_MAX_PX)
    n_rows = math.ceil(total_h_px / WCS_MAX_PX)

    if n_cols == 1 and n_rows == 1:
        print(f"  Downloading DEM ({total_w_px}×{total_h_px} px) …", flush=True)
        _download_dem_tile(bbox_wsen, output_path, resolution_m)
        print(f"  Saved → {output_path}")
        return

    print(f"  Downloading DEM ({total_w_px}×{total_h_px} px) in {n_cols}×{n_rows} tiles …", flush=True)
    lon_step = (east  - west)  / n_cols
    lat_step = (north - south) / n_rows

    tile_files = []
    try:
        for row in range(n_rows):
            for col in range(n_cols):
                t_west  = west  + col       * lon_step
                t_east  = west  + (col + 1) * lon_step
                t_south = south + row       * lat_step
                t_north = south + (row + 1) * lat_step
                tile_bbox = (t_west, t_south, t_east, t_north)
                tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
                tmp.close()
                print(f"    tile ({row},{col}) …", end=" ", flush=True)
                _download_dem_tile(tile_bbox, tmp.name, resolution_m)
                print("ok")
                tile_files.append(tmp.name)

        sources = [rasterio.open(p) for p in tile_files]
        try:
            merged_data, merged_transform = rio_merge(sources)
            src_crs    = sources[0].crs
            src_nodata = sources[0].nodata
        finally:
            for s in sources:
                s.close()

        nodata_val = src_nodata if src_nodata is not None else -9999.0
        with rasterio.open(
            output_path, "w",
            driver="GTiff", height=merged_data.shape[1], width=merged_data.shape[2],
            count=1, dtype=merged_data.dtype, crs=src_crs,
            transform=merged_transform, nodata=nodata_val,
        ) as dst:
            dst.write(merged_data)
        print(f"  Saved → {output_path} ({merged_data.shape[2]}×{merged_data.shape[1]} px)")
    finally:
        for p in tile_files:
            if os.path.exists(p):
                os.unlink(p)


def download_dem_copernicus(bbox_wsen: tuple, output_path: str, resolution_m: int = 10):
    """
    Download Copernicus GLO-30 DEM tiles from AWS S3, crop to bbox, and save.
    Tiles are 1°×1° GeoTIFFs (~28 MB each), cached individually in CACHE_DIR.
    bbox_wsen: (west, south, east, north) in WGS-84 degrees.
    """
    import numpy as np
    from rasterio.merge import merge as rio_merge
    from rasterio.warp import reproject, Resampling
    from rasterio.transform import from_bounds

    west, south, east, north = bbox_wsen
    lat_c = (south + north) / 2
    width_m   = (east - west)  * 111_000 * math.cos(math.radians(lat_c))
    height_m  = (north - south) * 111_000
    width_px  = max(100, int(width_m  / resolution_m))
    height_px = max(100, int(height_m / resolution_m))

    # Determine which 1°×1° tiles overlap the bbox
    lat_indices = range(int(math.floor(south)), int(math.ceil(north)))
    lon_indices = range(int(math.floor(west)),  int(math.ceil(east)))

    tile_paths = []
    for lat_idx in lat_indices:
        for lon_idx in lon_indices:
            ns  = "N" if lat_idx >= 0 else "S"
            ew  = "E" if lon_idx >= 0 else "W"
            name = (f"Copernicus_DSM_COG_10_{ns}{abs(lat_idx):02d}_00"
                    f"_{ew}{abs(lon_idx):03d}_00_DEM")
            tile_cache = os.path.join(CACHE_DIR, f"cop30_{lat_idx:+04d}_{lon_idx:+05d}.tif")

            if not os.path.exists(tile_cache):
                url = f"{COPERNICUS_S3}/{name}/{name}.tif"
                print(f"  Downloading Copernicus tile {ns}{abs(lat_idx):02d}{ew}{abs(lon_idx):03d} …", flush=True)
                resp = requests.get(url, timeout=300, stream=True)
                resp.raise_for_status()
                with open(tile_cache, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        f.write(chunk)
                print(f"  Saved → {tile_cache}")
            else:
                print(f"  Using cached Copernicus tile: {tile_cache}")

            tile_paths.append(tile_cache)

    # Open tiles, merge if necessary, reproject/crop to bbox, save
    sources = [rasterio.open(p) for p in tile_paths]
    try:
        if len(sources) > 1:
            merged_data, merged_transform = rio_merge(sources)
        else:
            merged_data      = sources[0].read()
            merged_transform = sources[0].transform
        src_crs    = sources[0].crs
        src_nodata = sources[0].nodata
    finally:
        for s in sources:
            s.close()

    nodata_val = src_nodata if src_nodata is not None else -9999.0
    out_transform = from_bounds(west, south, east, north, width_px, height_px)
    out_data = np.full((1, height_px, width_px), nodata_val, dtype=np.float32)

    reproject(
        source=merged_data,
        destination=out_data,
        src_transform=merged_transform,
        src_crs=src_crs,
        dst_transform=out_transform,
        dst_crs="EPSG:4326",
        resampling=Resampling.bilinear,
        src_nodata=nodata_val,
        dst_nodata=nodata_val,
    )

    with rasterio.open(
        output_path, "w",
        driver="GTiff", height=height_px, width=width_px,
        count=1, dtype=np.float32, crs="EPSG:4326",
        transform=out_transform, nodata=nodata_val,
    ) as dst:
        dst.write(out_data)
    print(f"  Saved → {output_path} ({width_px}×{height_px} px)")


def sample_dem(dem_path: str, coords: list[tuple]) -> list[float | None]:
    """Sample elevation (metres) from a local GeoTIFF at (lat, lon) pairs."""
    with rasterio.open(dem_path) as src:
        nodata = src.nodata
        pts = [(lon, lat) for lat, lon in coords]  # rasterio wants (x=lon, y=lat)
        elevs = []
        for val in src.sample(pts):
            v = float(val[0])
            if nodata is not None and v == nodata:
                elevs.append(None)
            elif v <= 0 or v < -500:
                elevs.append(None)
            else:
                elevs.append(v)
    return elevs


def compute_face_slope_raster(dem_path: str):
    """
    Compute Horn-formula terrain gradient magnitude for the entire DEM.
    Returns (slope_deg_array, dz_dx_array, dz_dy_array, transform).
    dz_dx is d(elev)/d(east_meters), dz_dy is d(elev)/d(south_meters).
    Fall line direction in (east, north) space is (-dz_dx, dz_dy).
    """
    with rasterio.open(dem_path) as src:
        z         = src.read(1).astype(np.float64)
        nodata    = src.nodata
        transform = src.transform
        lat_c     = (src.bounds.bottom + src.bounds.top) / 2
        res_x_m   = abs(transform.a) * 111_000 * math.cos(math.radians(lat_c))
        res_y_m   = abs(transform.e) * 111_000

    if nodata is not None:
        z[z == nodata] = np.nan
    z[z <= -500] = np.nan

    p = np.pad(z, 1, mode="edge")
    with np.errstate(invalid="ignore"):
        dz_dx = ((p[:-2, 2:] + 2*p[1:-1, 2:] + p[2:, 2:]) -
                 (p[:-2, :-2] + 2*p[1:-1, :-2] + p[2:, :-2])) / (8 * res_x_m)
        dz_dy = ((p[2:, :-2] + 2*p[2:, 1:-1] + p[2:, 2:]) -
                 (p[:-2, :-2] + 2*p[:-2, 1:-1] + p[:-2, 2:])) / (8 * res_y_m)
        slope = np.degrees(np.arctan(np.sqrt(dz_dx**2 + dz_dy**2)))

    slope_arr = np.clip(slope, 0.0, 70.0).astype(np.float32)
    return slope_arr, dz_dx.astype(np.float32), dz_dy.astype(np.float32), transform


FACE_DIRECTION_THRESHOLD_DEG = 25   # ignore face slopes where fall line > this° from travel
_COS_FACE_THRESHOLD = math.cos(math.radians(FACE_DIRECTION_THRESHOLD_DEG))

def sample_face_slopes(face_arr, dz_dx_arr, dz_dy_arr, transform,
                       coords: list[tuple]) -> list[float | None]:
    """Sample face-slope degrees (Horn gradient) at (lat, lon) pairs.

    Points where the terrain fall line is >FACE_DIRECTION_THRESHOLD_DEG from the travel
    direction return None — those slopes are beside the skier, not underfoot.
    Fall line in (east, north): (-dz_dx, dz_dy) per the Horn convention used here.
    """
    nrows, ncols = face_arr.shape
    n = len(coords)
    results = []
    for i, (lat, lon) in enumerate(coords):
        col_i = int((lon - transform.c) / transform.a)
        row_i = int((lat - transform.f) / transform.e)
        if not (0 <= row_i < nrows and 0 <= col_i < ncols):
            results.append(None)
            continue
        v = float(face_arr[row_i, col_i])
        if np.isnan(v):
            results.append(None)
            continue

        if n <= 1:
            results.append(v)
            continue

        lat1, lon1 = coords[max(0, i - 1)]
        lat2, lon2 = coords[min(n - 1, i + 1)]
        cos_lat = math.cos(math.radians((lat1 + lat2) / 2))
        travel_e = (lon2 - lon1) * cos_lat
        travel_n = lat2 - lat1
        travel_mag = math.sqrt(travel_e ** 2 + travel_n ** 2)
        if travel_mag < 1e-10:
            results.append(v)
            continue

        dx = float(dz_dx_arr[row_i, col_i])
        dy = float(dz_dy_arr[row_i, col_i])
        fall_e = -dx
        fall_n = dy
        fall_mag = math.sqrt(fall_e ** 2 + fall_n ** 2)
        if fall_mag < 1e-10:
            results.append(v)
            continue

        cos_theta = abs(fall_e * travel_e + fall_n * travel_n) / (fall_mag * travel_mag)
        results.append(v if cos_theta >= _COS_FACE_THRESHOLD else None)
    return results


# ── OSM fetching ──────────────────────────────────────────────────────────────

def _overpass_fetch(bbox: str) -> dict:
    """Raw Overpass fetch with mirror fallback."""
    # Fetch all named downhill runs regardless of OSM difficulty tag —
    # OSM tags are unreliable (e.g. Gold Coast is tagged easy but skis blue).
    # We classify runs from the slope data instead.
    query = f"""
[out:json][timeout:120];
(
  way["piste:type"="downhill"]{bbox};
);
out body;
>;
out skel qt;
"""
    last_err = None
    for url in OVERPASS_URLS:
        try:
            print(f"    trying {url.split('/')[2]} …", end=" ", flush=True)
            resp = requests.post(url, data={"data": query}, timeout=90)
            resp.raise_for_status()
            print("ok")
            return resp.json()
        except Exception as e:
            print(f"failed ({e})")
            last_err = e
            time.sleep(3)
    raise RuntimeError(f"All Overpass mirrors failed. Last error: {last_err}")


def fetch_runs(resort_name: str, bbox: str) -> list[dict]:
    cached = load_json_cache(resort_name)
    if cached is not None:
        print(f"  Using cached OSM data ({len(cached)} runs)")
        return cached

    data  = _overpass_fetch(bbox)
    nodes = {e["id"]: (e["lat"], e["lon"])
             for e in data["elements"] if e["type"] == "node"}

    runs = []
    for e in data["elements"]:
        if e["type"] != "way":
            continue
        tags   = e.get("tags", {})
        name   = tags.get("name") or tags.get("piste:name")
        if not name:
            continue                          # skip unnamed ways entirely
        coords = [nodes[nid] for nid in e["nodes"] if nid in nodes]
        if len(coords) >= 3:
            runs.append({
                "id":         e["id"],
                "name":       name,
                "osm_difficulty": tags.get("piste:difficulty", "unknown"),
                "coords":     coords,
            })

    save_json_cache(resort_name, runs)
    return runs

# ── Spotlio supplement ────────────────────────────────────────────────────────

def _norm_name(s: str) -> str:
    return s.lower().replace("'", "").replace("\u2019", "").replace("-", " ").strip()


def fetch_spotlio_supplement(resort_name: str, uuid: str, osm_runs: list[dict]) -> list[dict]:
    """
    Fetch runs from the Spotlio API that are missing from osm_runs.
    Returns run dicts in the same format as fetch_runs() output.
    Results are cached in cache/{name}_spotlio.json.
    """
    cache_key = f"{resort_name.replace(' ', '_')}_spotlio"
    cached = load_json_cache(cache_key)
    if cached is not None:
        print(f"  Using cached Spotlio supplement ({len(cached)} runs)")
        return cached

    print("  Fetching Spotlio supplement …", flush=True)
    resp = requests.get(
        f"{SPOTLIO_BASE}/api/touristic-objects?resortUuid={uuid}",
        timeout=30,
    )
    resp.raise_for_status()
    items = resp.json().get("data", [])

    osm_norm = {_norm_name(r["name"]) for r in osm_runs}

    supplement = []
    skipped_sparse = 0
    for item in items:
        if not (isinstance(item.get("type"), dict) and item["type"].get("name") == "slope"):
            continue
        name   = item.get("name", "").strip()
        coords = item.get("map_coordinates") or []
        if _norm_name(name) in osm_norm:
            continue                          # already covered by OSM
        if len(coords) < MIN_SPOTLIO_PTS:
            skipped_sparse += 1
            continue
        # Spotlio stores [lon, lat]; pipeline expects (lat, lon)
        run_coords = [(lat, lon) for lon, lat in coords]
        supplement.append({
            "id":             f"spotlio:{item['uuid']}",
            "name":           name,
            "osm_difficulty": "unknown",      # classified from slope data
            "coords":         run_coords,
        })

    if skipped_sparse:
        print(f"    skipped {skipped_sparse} Spotlio runs with <{MIN_SPOTLIO_PTS} points")
    print(f"    {len(supplement)} Spotlio runs added")
    save_json_cache(cache_key, supplement)
    return supplement


# ── Run stitching ─────────────────────────────────────────────────────────────

STITCH_THRESHOLD_M = 50   # connect endpoints closer than this

def _min_endpoint_dist(w1: dict, w2: dict) -> float:
    c1, c2 = w1["coords"], w2["coords"]
    return min(
        haversine(*c1[ 0], *c2[ 0]),
        haversine(*c1[ 0], *c2[-1]),
        haversine(*c1[-1], *c2[ 0]),
        haversine(*c1[-1], *c2[-1]),
    )


def _try_chain(ways: list[dict]) -> list[tuple] | None:
    """
    Try to arrange *ways* into a single head-to-tail chain.
    Returns a list of (way, forward:bool) pairs, or None if no valid chain exists.
    """
    def ep(w, forward): return w["coords"][0] if forward else w["coords"][-1]

    def is_free(w, forward):
        """True if this endpoint is not connected to any other way."""
        pt = ep(w, forward)
        return all(
            haversine(*pt, *ep(other, True))  > STITCH_THRESHOLD_M and
            haversine(*pt, *ep(other, False)) > STITCH_THRESHOLD_M
            for other in ways if other is not w
        )

    def dfs(chain, remaining, tail):
        if not remaining:
            return chain
        for w in remaining:
            rest = [x for x in remaining if x is not w]
            for fwd in (True, False):
                if haversine(*tail, *ep(w, fwd)) <= STITCH_THRESHOLD_M:
                    result = dfs(chain + [(w, fwd)], rest, ep(w, not fwd))
                    if result is not None:
                        return result
        return None

    # Prefer starting from a "free" endpoint so we begin at a true run terminus
    for start_way in ways:
        rest = [w for w in ways if w is not start_way]
        for fwd in (True, False):
            if is_free(start_way, fwd):
                chain = dfs([(start_way, fwd)], rest, ep(start_way, not fwd))
                if chain is not None:
                    return chain

    # Fallback: no free endpoint found — try any starting point
    for start_way in ways:
        rest = [w for w in ways if w is not start_way]
        chain = dfs([(start_way, True)], rest, ep(start_way, False))
        if chain is not None:
            return chain

    return None


def stitch_runs(runs: list[dict]) -> list[dict]:
    """Merge same-name OSM ways whose endpoints are within STITCH_THRESHOLD_M."""
    from collections import defaultdict

    by_name: dict[str, list] = defaultdict(list)
    for r in runs:
        by_name[r["name"]].append(r)

    out = []
    for name, group in by_name.items():
        if len(group) == 1:
            out.append(group[0])
            continue

        # Connected-components via endpoint proximity
        n = len(group)
        adj = [[_min_endpoint_dist(group[i], group[j]) <= STITCH_THRESHOLD_M
                for j in range(n)] for i in range(n)]

        visited = [False] * n
        components: list[list[dict]] = []
        for start in range(n):
            if visited[start]:
                continue
            stack, comp = [start], []
            while stack:
                node = stack.pop()
                if visited[node]: continue
                visited[node] = True
                comp.append(group[node])
                stack.extend(j for j in range(n) if adj[node][j] and not visited[j])
            components.append(comp)

        for comp in components:
            if len(comp) == 1:
                out.append(comp[0])
                continue

            chain = _try_chain(comp)
            if chain is None:
                out.extend(comp)
                continue

            merged_coords: list = []
            for i, (w, fwd) in enumerate(chain):
                coords = w["coords"] if fwd else list(reversed(w["coords"]))
                merged_coords.extend(coords if i == 0 else coords[1:])

            total_m = sum(
                haversine(*merged_coords[i], *merged_coords[i+1])
                for i in range(len(merged_coords)-1)
            )
            print(f"    stitched {len(comp)}× '{name}' → {total_m:.0f}m")
            out.append({
                "id":             chain[0][0]["id"],
                "name":           name,
                "osm_difficulty": chain[0][0]["osm_difficulty"],
                "coords":         merged_coords,
            })

    return out


# ── Geometry ──────────────────────────────────────────────────────────────────

def _point_in_polygon(lat, lon, ring):
    """Ray-casting point-in-polygon test."""
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        ri, ci = ring[i]
        rj, cj = ring[j]
        if ((ci > lon) != (cj > lon)) and \
                (lat < (rj - ri) * (lon - ci) / (cj - ci) + ri):
            inside = not inside
        j = i
    return inside


def profile_area(coords, dem_path, spacing_m=SAMPLE_SPACING_M):
    """
    For an area run (polygon without the closing duplicate), sample a 10m grid
    inside the polygon and follow the steepest-descent path from the highest
    interior point for the profile.  Also compute the globally-optimal steepest
    30 m pitch via DP (see _dp_steepest_30m_area).

    Returns (pts, elevs, dp_steepest) where dp_steepest is the DP-computed
    steepest value.  Returns (None, None, None) if the area is too small.
    """
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    lat_c = (lat_min + lat_max) / 2

    m_per_lat = 111_000
    m_per_lon = 111_000 * math.cos(math.radians(lat_c))
    dlat = spacing_m / m_per_lat
    dlon = spacing_m / m_per_lon

    # Build index grid
    lat_steps, v = [], lat_min
    while v <= lat_max + dlat:
        lat_steps.append(v); v += dlat
    lon_steps, v = [], lon_min
    while v <= lon_max + dlon:
        lon_steps.append(v); v += dlon

    grid   = {}   # (ri, ci) -> (lat, lon)
    pts_list = []
    idx_list = []
    for ri, lat in enumerate(lat_steps):
        for ci, lon in enumerate(lon_steps):
            if _point_in_polygon(lat, lon, coords):
                grid[(ri, ci)] = (lat, lon)
                idx_list.append((ri, ci))
                pts_list.append((lat, lon))

    if len(pts_list) < 5:
        return None, None, None

    elevs_list = sample_dem(dem_path, pts_list)
    elev_grid  = {idx: e for idx, e in zip(idx_list, elevs_list)
                  if e is not None and e > 0}

    if len(elev_grid) < 5:
        return None, None, None

    # Greedy steepest-descent from highest interior point
    start = max(elev_grid, key=lambda k: elev_grid[k])
    path, visited = [start], {start}
    DIRS = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]

    while True:
        ri, ci = path[-1]
        cur_pt, cur_e = grid[(ri, ci)], elev_grid[(ri, ci)]
        best, best_slope = None, 0.0
        for dr, dc in DIRS:
            nbr = (ri + dr, ci + dc)
            if nbr in visited or nbr not in elev_grid:
                continue
            d = haversine(*cur_pt, *grid[nbr])
            if d == 0:
                continue
            s = (cur_e - elev_grid[nbr]) / d
            if s > best_slope:
                best_slope, best = s, nbr
        if best is None:
            break
        path.append(best); visited.add(best)

    if len(path) < 3:
        return None, None, None

    pts         = [grid[i] for i in path]
    elevs       = [elev_grid[i] for i in path]
    dp_steepest = _dp_steepest_30m_area(elev_grid, grid)
    return pts, elevs, dp_steepest


def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    phi1, phi2 = radians(lat1), radians(lat2)
    a = (sin(radians(lat2 - lat1) / 2) ** 2
         + cos(phi1) * cos(phi2) * sin(radians(lon2 - lon1) / 2) ** 2)
    return 2 * R * asin(sqrt(a))


def interpolate_run(coords: list[tuple], spacing: float) -> list[tuple]:
    """Resample a polyline to evenly-spaced points (metres)."""
    out = [coords[0]]
    carry = 0.0
    for i in range(1, len(coords)):
        seg = haversine(*coords[i - 1], *coords[i])
        if seg == 0:
            continue
        lat1, lon1 = coords[i - 1]
        lat2, lon2 = coords[i]
        t = carry / seg
        while t < 1.0:
            out.append((lat1 + t * (lat2 - lat1),
                         lon1 + t * (lon2 - lon1)))
            t += spacing / seg
        carry = (t - 1.0) * seg
    if haversine(*out[-1], *coords[-1]) > spacing / 2:
        out.append(coords[-1])
    return out

# ── Slope profile ─────────────────────────────────────────────────────────────

def slope_profile(coords, elevs, smooth_pts: int):
    """
    Returns (cum_dist_m, slope_deg) arrays for one run.
    Positive = downhill.
    """
    # Cumulative distance
    dists = [0.0]
    for i in range(1, len(coords)):
        dists.append(dists[-1] + haversine(*coords[i - 1], *coords[i]))
    cum = np.array(dists)

    # Drop points with missing elevation
    mask = np.array([e is not None for e in elevs])
    if mask.sum() < 4:
        return None, None
    cum_c   = cum[mask]
    elev_c  = np.array([e for e in elevs if e is not None], dtype=float)

    # Orient top → bottom
    if elev_c[0] < elev_c[-1]:
        cum_c  = cum_c[-1] - cum_c[::-1]
        elev_c = elev_c[::-1]

    # Smooth elevation — use mode="valid" to avoid zero-padding artifacts at
    # run edges; trim cum_c to match the shorter output.
    if smooth_pts > 1:
        kernel = np.ones(smooth_pts) / smooth_pts
        half   = smooth_pts // 2
        elev_c = np.convolve(elev_c, kernel, mode="valid")
        cum_c  = cum_c[half: half + len(elev_c)]
        if len(cum_c) < 2:
            return None, None

    # Per-segment slope
    horiz = np.diff(cum_c)
    vert  = -np.diff(elev_c)           # positive = descending
    with np.errstate(divide="ignore", invalid="ignore"):
        slope_deg = np.where(
            horiz > 0,
            np.degrees(np.arctan2(np.abs(vert), horiz)) * np.sign(vert),
            0.0,
        )

    seg_mid = (cum_c[:-1] + cum_c[1:]) / 2

    # Clip physically impossible values — anything above 55° is a bad OSM node
    # or DEM artefact (SteepSeeker's top category is 47°+).
    slope_deg = np.clip(slope_deg, -5.0, 55.0)

    return seg_mid, slope_deg


def steepest_30m(slope_deg: np.ndarray, spacing_m: int = SAMPLE_SPACING_M) -> float:
    """Max rolling-mean slope over STEEPEST_WINDOW_M on downhill segments."""
    w = max(2, STEEPEST_WINDOW_M // spacing_m)
    down = slope_deg[slope_deg > 0]
    if len(down) < w:
        return float(np.max(down)) if len(down) > 0 else 0.0
    rolling = np.convolve(down, np.ones(w) / w, mode="valid")
    return float(np.max(rolling))


FACE_SMOOTH_WINDOW_M = 10   # rolling-mean window for face steepest (metres)

def face_steepest_30m(face_slopes: list, spacing_m: int = SAMPLE_SPACING_M) -> float:
    """Max direction-filtered face slope (Horn gradient magnitude) along the run.
    Uses a rolling mean over FACE_SMOOTH_WINDOW_M to require that steep terrain
    persists across several consecutive samples rather than a single noisy pixel."""
    valid = np.array([s if s is not None else 0.0 for s in face_slopes], dtype=float)
    valid = np.clip(valid, 0.0, 55.0)
    w = max(2, FACE_SMOOTH_WINDOW_M // spacing_m)
    down = valid[valid > 0]
    if len(down) < w:
        return float(np.max(down)) if len(down) > 0 else 0.0
    rolling = np.convolve(down, np.ones(w) / w, mode="valid")
    return float(np.max(rolling))


def get_steepest(slope_deg: np.ndarray | None, override: float | None, spacing_m: int = SAMPLE_SPACING_M) -> float:
    """Return the steepest-30m value, preferring an explicit override (used for
    area runs where the DP-computed value is more accurate than the single
    greedy-path profile)."""
    if override is not None:
        return override
    if slope_deg is None:
        return 0.0
    return steepest_30m(slope_deg, spacing_m)


def _dp_steepest_30m_area(elev_grid: dict, grid: dict) -> float:
    """Compute steepest_30m for an area by searching over ALL possible 3-step
    downhill paths in the area grid using dynamic programming.

    The greedy-descent approach (used for the profile path) starts from a single
    point and can miss the steepest 30 m pitch if it lies in a different part of
    the area.  This DP finds the globally optimal pitch in O(8 * n) time.

    Args:
        elev_grid: {(ri, ci): elevation_m} for valid interior cells.
        grid:      {(ri, ci): (lat, lon)} for those same cells.
    Returns:
        Maximum mean slope (degrees) over any 3-step strictly-downhill path.
    """
    DIRS = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]

    if len(elev_grid) < 4:
        return 0.0

    # Process cells high → low so that when we visit cell C, all cells higher
    # than C (its potential predecessors) have already been processed.
    sorted_cells = sorted(elev_grid.keys(), key=lambda k: elev_grid[k], reverse=True)

    # best_sum_k[cell] = maximum sum of slopes for a k-step strictly-downhill
    # path that ENDS at `cell`.
    best1: dict = {}
    best2: dict = {}
    best3: dict = {}

    for cell in sorted_cells:
        ri, ci = cell
        e_cell  = elev_grid[cell]
        pt_cell = grid[cell]

        s1_best = s2_best = s3_best = -math.inf

        for dr, dc in DIRS:
            prev = (ri + dr, ci + dc)
            if prev not in elev_grid:
                continue
            e_prev = elev_grid[prev]
            if e_prev <= e_cell:          # must descend (strictly downhill)
                continue

            d = haversine(*grid[prev], *pt_cell)
            if d == 0:
                continue
            slope = min(55.0, math.degrees(math.atan2(e_prev - e_cell, d)))

            if slope > s1_best:
                s1_best = slope

            if prev in best1:
                v = best1[prev] + slope
                if v > s2_best:
                    s2_best = v

            if prev in best2:
                v = best2[prev] + slope
                if v > s3_best:
                    s3_best = v

        if s1_best > -math.inf:
            best1[cell] = s1_best
        if s2_best > -math.inf:
            best2[cell] = s2_best
        if s3_best > -math.inf:
            best3[cell] = s3_best

    if best3:
        return max(best3.values()) / 3.0
    if best2:
        return max(best2.values()) / 2.0
    if best1:
        return max(best1.values())
    return 0.0

# ── Plotting ──────────────────────────────────────────────────────────────────

RUN_H_IN  = 0.9    # inches per run row
SEP_H_IN  = 0.30   # inches per tier-separator row

# SteepSeeker thresholds (descending)
TIERS = [
    (36, "#333333", "-."),   # black/expert boundary
    (27, "#2196F3", "--"),   # blue/black boundary
    (18, "#4CAF50", ":"),    # green/blue boundary
]

def _tier(deg: float) -> int:
    for threshold, _, _ in TIERS:
        if deg >= threshold:
            return threshold
    return 0


def _sorted_with_separators(results: list) -> list:
    """Sort runs by steepest section descending; insert None at tier breaks."""
    valid = [(n, d, s, o, f) for n, d, s, o, f in results if s is not None]
    valid.sort(key=lambda x: get_steepest(x[2], x[3]), reverse=True)

    out, prev_tier = [], None
    for run in valid:
        t = _tier(get_steepest(run[2], run[3]))
        if prev_tier is not None and t != prev_tier:
            out.append(None)          # tier separator
        out.append(run)
        prev_tier = t
    return out


def build_figure(all_results: dict) -> plt.Figure:
    resort_names = [r["name"] for r in RESORTS]
    colors       = {r["name"]: r["color"] for r in RESORTS}

    col_rows = {rname: _sorted_with_separators(all_results[rname])
                for rname in resort_names}

    if all(len(v) == 0 for v in col_rows.values()):
        raise ValueError("No valid runs to plot.")

    # Shared x-limit
    all_dists = [d[-1] for rows in col_rows.values()
                 for item in rows if item is not None
                 for _, d, *_ in [item]]
    x_max = max(all_dists) / 1000 * 1.05

    # ── Absolute layout in inches ────────────────────────────────────────────
    # Guarantees every run row is exactly RUN_H_IN tall regardless of how many
    # separator rows each column has.
    TITLE_H  = 0.45
    XLABEL_H = 0.35
    L_LABEL  = 2.2   # label area left of left chart
    R_LABEL  = 2.5   # label area left of right chart (in the middle gap)
    CHART_W  = 6.5   # chart width, both columns
    R_MARGIN = 0.3
    FIG_W    = L_LABEL + CHART_W + R_LABEL + CHART_W + R_MARGIN  # = 18.0

    def col_h(rows):
        return sum(RUN_H_IN if r is not None else SEP_H_IN for r in rows)

    fig_h = max(col_h(rows) for rows in col_rows.values()) + TITLE_H + XLABEL_H

    # x positions of the two chart areas (in inches)
    col_x = [L_LABEL, L_LABEL + CHART_W + R_LABEL]

    fig = plt.figure(figsize=(FIG_W, fig_h))
    fig.suptitle(
        "Run Slope Profiles — Palisades Tahoe vs Northstar",
        fontsize=12, fontweight="bold", y=(fig_h - TITLE_H * 0.4) / fig_h,
    )

    for col_idx, rname in enumerate(resort_names):
        rows  = col_rows[rname]
        color = colors[rname]
        if not rows:
            continue

        cx    = col_x[col_idx]
        # start drawing just below the title
        y_cur = fig_h - TITLE_H

        first_ax = None
        run_axes = []

        for item in rows:
            row_h = RUN_H_IN if item is not None else SEP_H_IN
            y_bot = y_cur - row_h

            # Convert inches → figure fractions
            rect = [cx / FIG_W, y_bot / fig_h,
                    CHART_W / FIG_W, row_h / fig_h]

            if item is None:
                ax = fig.add_axes(rect)
                ax.axis("off")
                ax.plot([0, 1], [0.5, 0.5], color="#cccccc", linewidth=0.6,
                        transform=ax.transAxes)
                y_cur -= row_h
                continue

            ax = fig.add_axes(rect, sharey=first_ax)

            if first_ax is None:
                first_ax = ax
                ax.set_title(rname, fontsize=10, fontweight="bold", pad=3)

            run_name, dist_m, slope_deg, steepest_override, _face_steep = item
            steepest = get_steepest(slope_deg, steepest_override)

            ax.fill_between(dist_m / 1000, slope_deg, 0,
                            where=(slope_deg >= 0),
                            alpha=0.45, color=color, linewidth=0)
            ax.plot(dist_m / 1000, slope_deg,
                    color=color, linewidth=0.8, alpha=0.9)
            for deg, ref_color, ls in TIERS:
                ax.axhline(deg, color=ref_color, linewidth=0.8,
                           linestyle=ls, alpha=0.6, zorder=0)

            ax.set_ylabel(
                f"{run_name}\n{steepest:.1f}°",
                rotation=0, ha="right", va="center",
                fontsize=7, labelpad=6,
            )
            ax.set_xlim(0, x_max)
            ax.set_ylim(-2, 50)
            ax.tick_params(labelsize=6, labelbottom=False)
            ax.grid(axis="x", alpha=0.2)
            run_axes.append(ax)
            y_cur -= row_h

        if run_axes:
            run_axes[-1].tick_params(labelbottom=True)
            run_axes[-1].set_xlabel("km from top", fontsize=7)

    return fig


# ── UI data export ───────────────────────────────────────────────────────────

UI_DATA_DIR = "ui/public/data"

def export_for_ui(all_results_by_smooth: dict, max_points: int = 150) -> None:
    """Write per-resort, per-smooth-level JSON files for the Next.js UI."""
    os.makedirs(UI_DATA_DIR, exist_ok=True)

    # Build OSM id and difficulty maps once
    osm_id_by_resort         = {}
    osm_difficulty_by_resort = {}
    for resort in RESORTS:
        osm_runs = load_json_cache(resort["name"]) or []
        osm_id_by_resort[resort["name"]]         = {r["name"]: r["id"] for r in osm_runs}
        osm_difficulty_by_resort[resort["name"]] = {r["name"]: r.get("osm_difficulty") for r in osm_runs}

    # Index always lists every configured resort so the UI knows what's available
    index = [
        {"name": r["name"], "slug": r["name"].lower().replace(" ", "_"),
         "color": r["color"], "smooth_levels": SMOOTH_LEVELS}
        for r in RESORTS
    ]
    with open(os.path.join(UI_DATA_DIR, "index.json"), "w") as f:
        json.dump(index, f)

    for resort in RESORTS:
        name  = resort["name"]
        color = resort["color"]
        slug  = name.lower().replace(" ", "_")
        osm_id_by_name         = osm_id_by_resort.get(name, {})
        osm_difficulty_by_name = osm_difficulty_by_resort.get(name, {})

        # Skip resorts that weren't processed this run
        if not any(name in results for results in all_results_by_smooth.values()):
            continue

        for smooth_pts, all_results in all_results_by_smooth.items():
            if name not in all_results:
                continue
            rows = _sorted_with_separators(all_results[name])
            runs_out = []

            for item in rows:
                if item is None:
                    runs_out.append(None)
                    continue
                run_name, dist_m, slope_deg, steepest_override, face_steep = item
                steepest = get_steepest(slope_deg, steepest_override)

                n    = len(dist_m)
                step = max(1, n // max_points)
                idx  = list(range(0, n, step))
                profile = [[round(float(dist_m[i]) / 1000, 3),
                            round(float(slope_deg[i]), 2)] for i in idx]

                run_entry = {
                    "name":      run_name,
                    "steepest":  round(steepest, 1),
                    "length_km": round(float(dist_m[-1]) / 1000, 2),
                    "profile":   profile,
                }
                if face_steep is not None:
                    delta = face_steep - steepest
                    run_entry["face_steepest"] = round(face_steep, 1)
                    run_entry["face_delta"]    = round(delta, 1)
                    run_entry["is_traverse"]   = delta >= TRAVERSE_DELTA_THRESHOLD
                osm_id = osm_id_by_name.get(run_name)
                if osm_id is not None:
                    run_entry["osm_id"] = osm_id
                osm_diff = osm_difficulty_by_name.get(run_name)
                if osm_diff:
                    run_entry["osm_difficulty"] = osm_diff
                runs_out.append(run_entry)

            out_path = os.path.join(UI_DATA_DIR, f"{slug}_s{smooth_pts}.json")
            with open(out_path, "w") as f:
                json.dump({"name": name, "color": color, "runs": runs_out}, f)
            print(f"  UI data → {out_path}  ({len([r for r in runs_out if r])} runs)")



# ── Lift fetching & export ───────────────────────────────────────────────────

def fetch_lifts(resort_name: str, bbox: str) -> list[dict]:
    cache_name = f"{resort_name}_lifts"
    cached = load_json_cache(cache_name)
    if cached is not None:
        print(f"  Using cached lift data ({len(cached)} lifts)")
        return cached

    query = f"""
[out:json][timeout:60];
(
  way["aerialway"]{bbox};
);
out body;
>;
out skel qt;
"""
    last_err = None
    for url in OVERPASS_URLS:
        try:
            print(f"    trying {url.split('/')[2]} …", end=" ", flush=True)
            resp = requests.post(url, data={"data": query}, timeout=60)
            resp.raise_for_status()
            print("ok")
            data = resp.json()
            break
        except Exception as e:
            print(f"failed ({e})")
            last_err = e
            time.sleep(3)
    else:
        raise RuntimeError(f"All Overpass mirrors failed: {last_err}")

    nodes = {e["id"]: (e["lat"], e["lon"])
             for e in data["elements"] if e["type"] == "node"}

    lifts = []
    for e in data["elements"]:
        if e["type"] != "way":
            continue
        tags  = e.get("tags", {})
        name  = tags.get("name")
        if not name:
            continue
        coords = [nodes[nid] for nid in e["nodes"] if nid in nodes]
        if len(coords) >= 2:
            lifts.append({
                "name":   name,
                "type":   tags.get("aerialway", "unknown"),
                "coords": coords,
            })

    save_json_cache(cache_name, lifts)
    return lifts


def stitch_lifts(lifts: list[dict]) -> list[dict]:
    """Merge same-name lift OSM ways into a single LineString (same logic as runs)."""
    from collections import defaultdict
    by_name: dict[str, list] = defaultdict(list)
    for l in lifts:
        by_name[l["name"]].append(l)

    out = []
    for name, group in by_name.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        chain = _try_chain(group)
        if chain is None:
            # Can't chain — keep the longest segment, drop the rest
            out.append(max(group, key=lambda g: len(g["coords"])))
            continue
        merged: list = []
        for i, (w, fwd) in enumerate(chain):
            coords = w["coords"] if fwd else list(reversed(w["coords"]))
            merged.extend(coords if i == 0 else coords[1:])
        print(f"    stitched {len(group)}× lift '{name}'")
        out.append({"name": name, "type": group[0]["type"], "coords": merged})
    return out


def export_lifts_geo_json(resort: dict, lifts: list) -> None:
    slug = resort["name"].lower().replace(" ", "_")
    path = os.path.join(UI_DATA_DIR, f"{slug}_lifts.json")
    lifts = stitch_lifts(lifts)
    features = []
    for lift in lifts:
        coords = [[round(lon, 6), round(lat, 6)] for lat, lon in lift["coords"]]
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"name": lift["name"], "type": lift["type"]},
        })
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    print(f"  Lifts GeoJSON → {path}  ({len(features)} lifts)")


# ── Geo JSON export (for map view) ───────────────────────────────────────────

def export_geo_json(resort: dict, runs_meta: list, raw_samples: list, profiles_s3: list, spacing_m: int = SAMPLE_SPACING_M) -> None:
    """Write a slope-coloured GeoJSON file for the resort map view.

    Slope coloring uses the same smooth=3 methodology as the profile data
    (3-point elevation smoothing → 10m slopes → subsample every 3rd point)
    so map colors are consistent with the steepest-degree values in the sidebar.
    """
    slug = resort["name"].lower().replace(" ", "_")
    path = os.path.join(UI_DATA_DIR, f"{slug}_geo.json")

    # Use ordered queues per name so duplicate-named runs each get their own
    # steepest value rather than the last one overwriting all previous entries.
    from collections import defaultdict as _dd
    steepest_queue    = _dd(list)
    face_steep_queue  = _dd(list)
    for run_name, _, slope, override, face_steep in profiles_s3:
        steepest_queue[run_name].append(
            get_steepest(slope, override) if slope is not None else 0.0
        )
        face_steep_queue[run_name].append(face_steep)

    osm_id_queue         = _dd(list)
    osm_difficulty_queue = _dd(list)
    orig_coords_queue    = _dd(list)
    for r in runs_meta:
        osm_id_queue[r["name"]].append(r["id"])
        osm_difficulty_queue[r["name"]].append(r.get("osm_difficulty"))
        orig_coords_queue[r["name"]].append(r["coords"])

    def _pop_steepest(name):
        q = steepest_queue.get(name, [])
        return q.pop(0) if q else 0.0

    def _pop_face_steep(name):
        q = face_steep_queue.get(name, [])
        return q.pop(0) if q else None

    def _pop_osm_id(name):
        q = osm_id_queue.get(name, [])
        return q.pop(0) if q else None

    def _pop_osm_difficulty(name):
        q = osm_difficulty_queue.get(name, [])
        return q.pop(0) if q else None

    def _pop_coords(name):
        q = orig_coords_queue.get(name, [])
        return q.pop(0) if q else []

    features = []

    for run_name, pts_10m, elevs_10m, _dp, face_slp in raw_samples:
        steepest       = _pop_steepest(run_name)
        _pop_face_steep(run_name)   # consume queue entry; recompute from direction-filtered slp
        osm_id         = _pop_osm_id(run_name)
        osm_difficulty = _pop_osm_difficulty(run_name)
        coords_orig    = _pop_coords(run_name)

        face_steep  = face_steepest_30m(face_slp, spacing_m) if face_slp is not None else None
        face_delta  = (face_steep - steepest) if face_steep is not None else None
        is_traverse = face_delta is not None and face_delta >= TRAVERSE_DELTA_THRESHOLD

        # Area runs: export polygon boundary from original OSM coords
        if len(coords_orig) > 2 and coords_orig[0] == coords_orig[-1]:
            ring = [[round(lon, 6), round(lat, 6)] for lat, lon in coords_orig]
            props = {
                "name":           run_name,
                "steepest":       round(steepest, 1),
                "osm_id":         osm_id,
                "osm_difficulty": osm_difficulty,
                "is_area":        True,
                "slopes":         [],
            }
            if face_steep is not None:
                props["face_steepest"] = round(face_steep, 1)
                props["is_traverse"]   = is_traverse
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": props,
            })
            continue

        # Drop None elevations; carry face slopes through the same filter
        face_list = face_slp if face_slp is not None else [None] * len(pts_10m)
        valid = [(p, e, f) for p, e, f in zip(pts_10m, elevs_10m, face_list)
                 if e is not None]
        if len(valid) < 5:
            continue
        pts_v     = tuple(x[0] for x in valid)
        elevs_arr = np.array([x[1] for x in valid], dtype=float)
        face_v    = [x[2] for x in valid]

        # Orient top-to-bottom
        if elevs_arr[0] < elevs_arr[-1]:
            pts_v     = pts_v[::-1]
            elevs_arr = elevs_arr[::-1]
            face_v    = face_v[::-1]

        # Smooth elevation with 30m window (matches SteepSeeker methodology)
        geo_w  = max(3, 30 // spacing_m)
        kernel = np.ones(geo_w) / geo_w
        half   = geo_w // 2
        elev_s = np.convolve(elevs_arr, kernel, mode="valid")   # len - (geo_w-1)
        pts_s  = pts_v[half: half + len(elev_s)]                # aligned slice
        if len(pts_s) < 2:
            continue

        # Compute directional slopes from smoothed elevation
        slopes_seg = []
        for i in range(len(pts_s) - 1):
            d  = haversine(*pts_s[i], *pts_s[i + 1])
            if d == 0:
                slopes_seg.append(0.0)
                continue
            dh = float(elev_s[i] - elev_s[i + 1])
            deg = math.degrees(math.atan2(abs(dh), d)) * (1 if dh >= 0 else -1)
            slopes_seg.append(max(-5.0, min(55.0, deg)))

        slopes_arr    = np.array(slopes_seg)
        slopes_smooth = (np.convolve(slopes_arr, kernel, mode="same")
                         if len(slopes_arr) >= geo_w else slopes_arr)

        # Face slopes: smooth over FACE_SMOOTH_WINDOW_M (10m) to eliminate
        # single-pixel DEM noise, then cap how much face can exceed line slope.
        # The cap (= FACE_DISPLAY_CAP) handles traverse artifacts where
        # face >> line without over-smoothing real steep sections where face ≈ line.
        face_raw  = np.array([f if f is not None else 0.0 for f in face_v], dtype=float)
        face_raw  = np.clip(face_raw, 0.0, 55.0)
        face_w    = max(2, FACE_SMOOTH_WINDOW_M // spacing_m)
        face_kern = np.ones(face_w) / face_w
        face_raw  = (np.convolve(face_raw, face_kern, mode="same")
                     if len(face_raw) >= face_w else face_raw)
        face_trim = face_raw[half: half + len(slopes_smooth)]  # align lengths

        # Cap: face can exceed line by at most FACE_DISPLAY_CAP degrees.
        # On a true steep descent face ≈ line so the cap is inactive; on a
        # traverse the cap prevents off-trail terrain from coloring segments wrong.
        face_trim = np.minimum(face_trim, slopes_smooth + FACE_DISPLAY_CAP)

        # Face slopes floored by line slopes: direction-filtered zeros fall back
        # to the directional value rather than pulling the segment down.
        use_face = face_slp is not None and len(face_trim) == len(slopes_smooth)
        display_slopes = np.maximum(face_trim, slopes_smooth) if use_face else slopes_smooth

        # Subsample: color each 30m segment by peak within window
        step    = GEO_SEGMENT_STEP
        indices = list(range(0, len(display_slopes), step))
        seg_slopes = [
            round(float(np.max(display_slopes[i: i + step])), 1)
            for i in indices
        ]
        seg_line_slopes = [
            round(float(np.max(slopes_smooth[i: i + step])), 1)
            for i in indices
        ]
        coord_pts = [pts_s[i] for i in indices] + [pts_s[-1]]
        coords    = [[round(lon, 6), round(lat, 6)] for lat, lon in coord_pts]

        props = {
            "name":           run_name,
            "steepest":       round(steepest, 1),
            "osm_id":         osm_id,
            "osm_difficulty": osm_difficulty,
            "slopes":         seg_slopes,       # face steepness per segment (line if no face data)
            "line_slopes":    seg_line_slopes,  # directional steepness per segment
        }
        if face_steep is not None:
            props["face_steepest"] = round(face_steep, 1)
            props["is_traverse"]   = is_traverse

        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": props,
        })

    geojson = {
        "type":    "FeatureCollection",
        "resort":  resort["name"],
        "color":   resort["color"],
        "features": features,
    }
    with open(path, "w") as f:
        json.dump(geojson, f)
    print(f"  Geo JSON → {path}  ({len(features)} runs)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--resort", metavar="NAME",
                        help="Process only this resort (default: all)")
    args = parser.parse_args()

    if args.resort:
        resorts = [r for r in RESORTS
                   if r["name"].lower() == args.resort.lower()]
        if not resorts:
            names = ", ".join(f'"{r["name"]}"' for r in RESORTS)
            print(f"Resort not found. Available: {names}")
            return
    else:
        resorts = RESORTS

    all_results_by_smooth: dict[int, dict] = {s: {} for s in SMOOTH_LEVELS}

    for resort in resorts:
        name      = resort["name"]
        spacing_m = resort["dem_resolution_m"]
        print(f"\n▶ {name}")

        # 1. DEM (download once, cache locally)
        res_m = spacing_m
        tif = dem_path_for(name, res_m)
        if not os.path.exists(tif):
            if resort.get("dem_source") == "copernicus":
                download_dem_copernicus(resort["dem_bbox"], tif, res_m)
            else:
                download_dem(resort["dem_bbox"], tif, res_m)
        else:
            print(f"  Using cached DEM: {tif}")

        # 2. Runs from OSM
        print("  Fetching blue runs from OSM …", flush=True)
        runs = fetch_runs(name, resort["osm_bbox"])
        print(f"  {len(runs)} ways found")
        runs = stitch_runs(runs)
        print(f"  {len(runs)} runs after stitching")

        if resort.get("spotlio_uuid"):
            extra = fetch_spotlio_supplement(name, resort["spotlio_uuid"], runs)
            runs = runs + extra

        # 3. For each smooth level, load or compute profiles.
        #    DEM is sampled once (lazily) if any level needs computing.
        raw_samples = None   # (name, pts, elevs, dp_steepest, face_slp) per run
        face_arr    = None   # face-slope raster, computed alongside raw_samples

        for s in SMOOTH_LEVELS:
            results = load_profiles(name, s)
            if results is not None:
                print(f"  Using cached profiles s={s} ({len(results)} runs)")
            else:
                if raw_samples is None:
                    print("  Sampling DEM …", flush=True)
                    face_arr, dz_dx_arr, dz_dy_arr, face_transform = compute_face_slope_raster(tif)
                    raw_samples = []
                    for run in runs:
                        c = run["coords"]
                        is_area = len(c) > 2 and c[0] == c[-1]
                        if is_area:
                            pts, elevs, dp_steepest = profile_area(c[:-1], tif, spacing_m)
                            if pts is None:          # fallback to perimeter
                                pts         = interpolate_run(c, spacing_m)
                                elevs       = sample_dem(tif, pts)
                                dp_steepest = None
                        else:
                            pts         = interpolate_run(c, spacing_m)
                            elevs       = sample_dem(tif, pts)
                            dp_steepest = None
                        face_slp = (sample_face_slopes(face_arr, dz_dx_arr, dz_dy_arr,
                                                       face_transform, pts)
                                    if pts is not None else None)
                        raw_samples.append((run["name"], pts, elevs, dp_steepest, face_slp))

                    face_steep_by_run = {
                        run_name: face_steepest_30m(face_slp, spacing_m) if face_slp is not None else None
                        for run_name, _, _, _, face_slp in raw_samples
                    }

                results = []
                for run_name, pts, elevs, dp_steepest, _face_slp in raw_samples:
                    dist, slope = slope_profile(pts, elevs, max(1, s // spacing_m))
                    results.append((run_name, dist, slope, dp_steepest,
                                    face_steep_by_run.get(run_name)))
                    status = "ok" if dist is not None else "skip"
                    print(f"    s={s}  {run_name[:38]:<38}  [{status}]")
                save_profiles(name, s, results)

            all_results_by_smooth[s][name] = results

        # 4. Geo JSON for map view
        geo_out = os.path.join(UI_DATA_DIR, f"{name.lower().replace(' ', '_')}_geo.json")
        if not os.path.exists(geo_out):
            if raw_samples is None:
                print("  Sampling DEM for geo export …", flush=True)
                face_arr, dz_dx_arr, dz_dy_arr, face_transform = compute_face_slope_raster(tif)
                raw_samples = []
                for run in runs:
                    c = run["coords"]
                    is_area = len(c) > 2 and c[0] == c[-1]
                    if is_area:
                        pts, elevs, dp_steepest = profile_area(c[:-1], tif, spacing_m)
                        if pts is None:
                            pts         = interpolate_run(c, spacing_m)
                            elevs       = sample_dem(tif, pts)
                    else:
                        pts   = interpolate_run(run["coords"], spacing_m)
                        elevs = sample_dem(tif, pts)
                    face_slp = (sample_face_slopes(face_arr, dz_dx_arr, dz_dy_arr,
                                                   face_transform, pts)
                                if pts is not None else None)
                    raw_samples.append((run["name"], pts, elevs, None, face_slp))
            export_geo_json(resort, runs, raw_samples,
                            all_results_by_smooth[30].get(name, []), spacing_m)
        else:
            print(f"  Geo JSON cached: {geo_out}")

        # 5. Lift data for map view
        lifts_out = os.path.join(UI_DATA_DIR, f"{name.lower().replace(' ', '_')}_lifts.json")
        if not os.path.exists(lifts_out):
            print("  Fetching lift data from OSM …", flush=True)
            lifts = fetch_lifts(name, resort["osm_bbox"])
            print(f"  {len(lifts)} named lifts found")
            export_lifts_geo_json(resort, lifts)
        else:
            print(f"  Lifts cached: {lifts_out}")

    # 6. Export JSON for the web UI
    export_for_ui(all_results_by_smooth)

    # 7. Plot (only when exactly 2 resorts — build_figure is hardcoded for that)
    if len(resorts) != 2:
        return
    fig = build_figure(all_results_by_smooth[SMOOTH_POINTS])

    out = "blue_runs_comparison.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"\n✓ Saved → {out}")
    plt.close(fig)


if __name__ == "__main__":
    main()
