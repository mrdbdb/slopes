"""DEM download and sampling: USGS 3DEP, Copernicus GLO-30, swissALTI3D."""

import os
import math
import time

import numpy as np
import requests
import rasterio

from .constants import (
    CACHE_DIR, USGS_WCS_URL, COPERNICUS_S3, SWISSTOPO_STAC, GSI_DEM_URL,
)
from .cache import dem_path_for  # noqa: re-exported for callers

WCS_MAX_PX  = 2000   # safe per-dimension limit for USGS 3DEP WCS
WCS_RETRIES = 3

FACE_DIRECTION_THRESHOLD_DEG = 25
_COS_FACE_THRESHOLD = math.cos(math.radians(FACE_DIRECTION_THRESHOLD_DEG))


# ── USGS 3DEP ────────────────────────────────────────────────────────────────

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


# ── Copernicus GLO-30 ─────────────────────────────────────────────────────────

def download_dem_copernicus(bbox_wsen: tuple, output_path: str, resolution_m: int = 10):
    """
    Download Copernicus GLO-30 DEM tiles from AWS S3, crop to bbox, and save.
    Tiles are 1°×1° GeoTIFFs (~28 MB each), cached individually in CACHE_DIR.
    bbox_wsen: (west, south, east, north) in WGS-84 degrees.
    """
    from rasterio.merge import merge as rio_merge
    from rasterio.warp import reproject, Resampling
    from rasterio.transform import from_bounds

    west, south, east, north = bbox_wsen
    lat_c = (south + north) / 2
    width_m   = (east - west)  * 111_000 * math.cos(math.radians(lat_c))
    height_m  = (north - south) * 111_000
    width_px  = max(100, int(width_m  / resolution_m))
    height_px = max(100, int(height_m / resolution_m))

    lat_indices = range(int(math.floor(south)), int(math.ceil(north)))
    lon_indices = range(int(math.floor(west)),  int(math.ceil(east)))

    tile_paths = []
    for lat_idx in lat_indices:
        for lon_idx in lon_indices:
            ns   = "N" if lat_idx >= 0 else "S"
            ew   = "E" if lon_idx >= 0 else "W"
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

    nodata_val    = src_nodata if src_nodata is not None else -9999.0
    out_transform = from_bounds(west, south, east, north, width_px, height_px)
    out_data      = np.full((1, height_px, width_px), nodata_val, dtype=np.float32)

    reproject(
        source=merged_data, destination=out_data,
        src_transform=merged_transform, src_crs=src_crs,
        dst_transform=out_transform, dst_crs="EPSG:4326",
        resampling=Resampling.bilinear,
        src_nodata=nodata_val, dst_nodata=nodata_val,
    )

    with rasterio.open(
        output_path, "w",
        driver="GTiff", height=height_px, width=width_px,
        count=1, dtype=np.float32, crs="EPSG:4326",
        transform=out_transform, nodata=nodata_val,
    ) as dst:
        dst.write(out_data)
    print(f"  Saved → {output_path} ({width_px}×{height_px} px)")


# ── swissALTI3D (swisstopo) ───────────────────────────────────────────────────

def download_dem_swisstopo(bbox_wsen: tuple, output_path: str, resolution_m: int = 2):
    """
    Download swissALTI3D 2m DEM tiles from the swisstopo STAC API, merge,
    reproject to WGS84, and save as a GeoTIFF.

    Tiles are queried by bbox (WGS84) from the swisstopo open-data STAC API.
    Each tile is cached individually in CACHE_DIR so subsequent runs are instant.
    bbox_wsen: (west, south, east, north) in WGS-84 degrees.
    """
    from rasterio.merge import merge as rio_merge
    from rasterio.warp import reproject, Resampling
    from rasterio.transform import from_bounds

    west, south, east, north = bbox_wsen
    lat_c     = (south + north) / 2
    width_m   = (east - west)  * 111_000 * math.cos(math.radians(lat_c))
    height_m  = (north - south) * 111_000
    width_px  = max(100, int(width_m  / resolution_m))
    height_px = max(100, int(height_m / resolution_m))

    # Query STAC for tiles covering the bbox (paginate with limit=200)
    all_items = []
    url = SWISSTOPO_STAC
    params: dict = {"bbox": f"{west},{south},{east},{north}", "limit": 200}
    while url:
        resp = requests.get(url, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        all_items.extend(data.get("features", []))
        next_link = next((l for l in data.get("links", []) if l.get("rel") == "next"), None)
        url    = next_link["href"] if next_link else None
        params = {}   # next URL is already fully formed

    if not all_items:
        raise RuntimeError(f"No swissALTI3D tiles found for bbox {bbox_wsen}")

    print(f"  Found {len(all_items)} swissALTI3D tiles", flush=True)

    tile_paths = []
    for item in all_items:
        item_id    = item["id"]
        tile_cache = os.path.join(CACHE_DIR, f"swisstopo_{item_id}.tif")

        if not os.path.exists(tile_cache):
            asset_url = _pick_swisstopo_asset(item)
            if asset_url is None:
                print(f"  Warning: no GeoTIFF asset for {item_id}, skipping")
                continue
            print(f"  Downloading swisstopo tile {item_id} …", flush=True)
            r = requests.get(asset_url, timeout=300, stream=True)
            r.raise_for_status()
            with open(tile_cache, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
            print(f"  Saved → {tile_cache}")
        else:
            print(f"  Using cached swisstopo tile: {tile_cache}")

        tile_paths.append(tile_cache)

    if not tile_paths:
        raise RuntimeError("No swissALTI3D tiles could be downloaded")

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

    nodata_val    = src_nodata if src_nodata is not None else -9999.0
    out_transform = from_bounds(west, south, east, north, width_px, height_px)
    out_data      = np.full((1, height_px, width_px), nodata_val, dtype=np.float32)

    reproject(
        source=merged_data, destination=out_data,
        src_transform=merged_transform, src_crs=src_crs,
        dst_transform=out_transform, dst_crs="EPSG:4326",
        resampling=Resampling.bilinear,
        src_nodata=nodata_val, dst_nodata=nodata_val,
    )

    with rasterio.open(
        output_path, "w",
        driver="GTiff", height=height_px, width=width_px,
        count=1, dtype=np.float32, crs="EPSG:4326",
        transform=out_transform, nodata=nodata_val,
    ) as dst:
        dst.write(out_data)
    print(f"  Saved → {output_path} ({width_px}×{height_px} px)")


def _pick_swisstopo_asset(item: dict) -> str | None:
    """Return the best GeoTIFF asset URL from a swisstopo STAC item.

    Prefers assets whose key or title contains '2' (2m resolution) over
    coarser variants. Falls back to any .tif asset if no 2m is found.
    """
    assets = item.get("assets", {})
    tif_assets = {
        k: a for k, a in assets.items()
        if a.get("href", "").lower().endswith(".tif")
    }
    for key, asset in tif_assets.items():
        title = asset.get("title", "")
        if "2" in key or "2m" in title.lower():
            return asset["href"]
    if tif_assets:
        return next(iter(tif_assets.values()))["href"]
    return None


# ── GSI DEM5A (Japan) ─────────────────────────────────────────────────────────

_GSI_ZOOM     = 15
_GSI_ORIGIN_M = 20037508.342789244   # half Earth circumference in EPSG:3857 metres
_GSI_NODATA   = -9999.0


def _latlon_to_tile(lat_deg: float, lon_deg: float, z: int) -> tuple[int, int]:
    n     = 2 ** z
    x     = int((lon_deg + 180.0) / 360.0 * n)
    lat_r = math.radians(lat_deg)
    y     = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def _tile_bbox_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    tile_m = 2 * _GSI_ORIGIN_M / (2 ** z)
    west   = -_GSI_ORIGIN_M + x * tile_m
    north  =  _GSI_ORIGIN_M - y * tile_m
    return west, north - tile_m, west + tile_m, north   # (W, S, E, N)


def _decode_gsi_png(png_bytes: bytes) -> np.ndarray:
    """Decode a GSI elevation PNG into a (256×256) float32 array (metres).

    Encoding: x = R*65536 + G*256 + B
      x < 2^23  → elevation =  x * 0.01 m
      x > 2^23  → elevation = (x - 2^24) * 0.01 m   (negative terrain)
      x == 2^23 → no-data
    """
    with rasterio.MemoryFile(png_bytes) as mf:
        with mf.open() as src:
            data = src.read()
    r = data[0].astype(np.int32)
    g = data[1].astype(np.int32)
    b = data[2].astype(np.int32)
    x = r * 65536 + g * 256 + b
    elev = np.where(x < 2**23, x * 0.01, (x - 2**24) * 0.01).astype(np.float32)
    elev[x == 2**23] = _GSI_NODATA
    return elev


def _save_gsi_tile(path: str, elev: np.ndarray, z: int, tx: int, ty: int) -> None:
    from rasterio.transform import from_bounds
    tile_w, tile_s, tile_e, tile_n = _tile_bbox_3857(z, tx, ty)
    tile_tf = from_bounds(tile_w, tile_s, tile_e, tile_n, 256, 256)
    with rasterio.open(
        path, "w",
        driver="GTiff", height=256, width=256,
        count=1, dtype=np.float32, crs="EPSG:3857",
        transform=tile_tf, nodata=_GSI_NODATA,
    ) as dst:
        dst.write(elev[np.newaxis])


def _fetch_gsi_tile(z: int, tx: int, ty: int) -> str | None:
    """Return path to a cached GeoTIFF for tile (z, tx, ty).

    Tries DEM5A then DEM5B at zoom z. If both 404, falls back to the parent
    dem_png tile at zoom z-1, resampled to the zoom-z tile bounds.
    Returns None only if no data source is available.
    """
    from rasterio.transform import from_bounds
    from rasterio.warp import reproject, Resampling

    tile_cache = os.path.join(CACHE_DIR, f"gsi_{z}_{tx}_{ty}.tif")
    if os.path.exists(tile_cache):
        return tile_cache

    for layer in ("dem5a_png", "dem5b_png"):
        try:
            resp = requests.get(f"{GSI_DEM_URL}/{layer}/{z}/{tx}/{ty}.png", timeout=30)
            if resp.status_code == 200:
                elev = _decode_gsi_png(resp.content)
                _save_gsi_tile(tile_cache, elev, z, tx, ty)
                return tile_cache
        except Exception:
            continue

    # Fallback: zoom-(z-1) dem_png tile, resampled to zoom-z bounds
    z14, tx14, ty14 = z - 1, tx // 2, ty // 2
    parent_cache = os.path.join(CACHE_DIR, f"gsi_dem_png_{z14}_{tx14}_{ty14}.tif")
    if not os.path.exists(parent_cache):
        try:
            resp = requests.get(f"{GSI_DEM_URL}/dem_png/{z14}/{tx14}/{ty14}.png", timeout=30)
            if resp.status_code != 200:
                return None
            elev14 = _decode_gsi_png(resp.content)
            _save_gsi_tile(parent_cache, elev14, z14, tx14, ty14)
        except Exception:
            return None

    tile_w, tile_s, tile_e, tile_n = _tile_bbox_3857(z, tx, ty)
    tile_tf = from_bounds(tile_w, tile_s, tile_e, tile_n, 256, 256)
    out_data = np.full((1, 256, 256), _GSI_NODATA, dtype=np.float32)
    with rasterio.open(parent_cache) as src:
        reproject(
            source=rasterio.band(src, 1), destination=out_data,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=tile_tf, dst_crs="EPSG:3857",
            resampling=Resampling.bilinear,
            src_nodata=_GSI_NODATA, dst_nodata=_GSI_NODATA,
        )
    with rasterio.open(
        tile_cache, "w",
        driver="GTiff", height=256, width=256,
        count=1, dtype=np.float32, crs="EPSG:3857",
        transform=tile_tf, nodata=_GSI_NODATA,
    ) as dst:
        dst.write(out_data)
    return tile_cache


def download_dem_gsi(bbox_wsen: tuple, output_path: str, resolution_m: int = 5):
    """
    Download GSI DEM for Japan via PNG tiles, merge, reproject to WGS84.

    Per-tile priority: DEM5A zoom 15 (~5m) → DEM5B zoom 15 → dem_png zoom 14
    (~10m, nationwide coverage) resampled to zoom-15 bounds. This ensures full
    mountain coverage even where LiDAR/photogrammetry 5m data is absent.
    Tiles are cached as CACHE_DIR/gsi_15_{x}_{y}.tif.
    bbox_wsen: (west, south, east, north) in WGS-84 degrees.
    """
    from rasterio.merge import merge as rio_merge
    from rasterio.warp import reproject, Resampling
    from rasterio.transform import from_bounds

    west, south, east, north = bbox_wsen
    lat_c     = (south + north) / 2
    width_m   = (east - west)  * 111_000 * math.cos(math.radians(lat_c))
    height_m  = (north - south) * 111_000
    width_px  = max(100, int(width_m  / resolution_m))
    height_px = max(100, int(height_m / resolution_m))

    z          = _GSI_ZOOM
    max_tile   = 2**z - 1
    x_west, y_north = _latlon_to_tile(north, west, z)
    x_east, y_south = _latlon_to_tile(south, east, z)
    x_west  = max(0, min(x_west,  max_tile))
    x_east  = max(0, min(x_east,  max_tile))
    y_north = max(0, min(y_north, max_tile))
    y_south = max(0, min(y_south, max_tile))

    total = (x_east - x_west + 1) * (y_south - y_north + 1)
    print(f"  Downloading GSI DEM ({total} tiles at zoom {z}) …", flush=True)

    tile_paths = []
    done = 0
    for ty in range(y_north, y_south + 1):
        for tx in range(x_west, x_east + 1):
            path = _fetch_gsi_tile(z, tx, ty)
            if path:
                tile_paths.append(path)
            done += 1
            if done % 50 == 0 or done == total:
                print(f"    {done}/{total} tiles …", flush=True)

    if not tile_paths:
        raise RuntimeError("No GSI DEM tiles could be downloaded")

    sources = [rasterio.open(p) for p in tile_paths]
    try:
        if len(sources) > 1:
            merged_data, merged_transform = rio_merge(sources, nodata=_GSI_NODATA)
        else:
            merged_data      = sources[0].read()
            merged_transform = sources[0].transform
        src_crs = sources[0].crs
    finally:
        for s in sources:
            s.close()

    out_transform = from_bounds(west, south, east, north, width_px, height_px)
    out_data      = np.full((1, height_px, width_px), _GSI_NODATA, dtype=np.float32)

    reproject(
        source=merged_data, destination=out_data,
        src_transform=merged_transform, src_crs=src_crs,
        dst_transform=out_transform, dst_crs="EPSG:4326",
        resampling=Resampling.bilinear,
        src_nodata=_GSI_NODATA, dst_nodata=_GSI_NODATA,
    )

    with rasterio.open(
        output_path, "w",
        driver="GTiff", height=height_px, width=width_px,
        count=1, dtype=np.float32, crs="EPSG:4326",
        transform=out_transform, nodata=_GSI_NODATA,
    ) as dst:
        dst.write(out_data)
    print(f"  Saved → {output_path} ({width_px}×{height_px} px)")


# ── DEM sampling ─────────────────────────────────────────────────────────────

def sample_dem(dem_path: str, coords: list[tuple]) -> list[float | None]:
    """Sample elevation (metres) from a local GeoTIFF at (lat, lon) pairs."""
    with rasterio.open(dem_path) as src:
        nodata = src.nodata
        pts    = [(lon, lat) for lat, lon in coords]
        elevs  = []
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


def sample_face_slopes(face_arr, dz_dx_arr, dz_dy_arr, transform,
                       coords: list[tuple]) -> list[float | None]:
    """Sample face-slope degrees (Horn gradient) at (lat, lon) pairs.

    Points where the terrain fall line is >FACE_DIRECTION_THRESHOLD_DEG from the
    travel direction return None — those slopes are beside the skier, not underfoot.
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
        cos_lat  = math.cos(math.radians((lat1 + lat2) / 2))
        travel_e = (lon2 - lon1) * cos_lat
        travel_n = lat2 - lat1
        travel_mag = math.sqrt(travel_e**2 + travel_n**2)
        if travel_mag < 1e-10:
            results.append(v)
            continue

        dx      = float(dz_dx_arr[row_i, col_i])
        dy      = float(dz_dy_arr[row_i, col_i])
        fall_e  = -dx
        fall_n  = dy
        fall_mag = math.sqrt(fall_e**2 + fall_n**2)
        if fall_mag < 1e-10:
            results.append(v)
            continue

        cos_theta = abs(fall_e * travel_e + fall_n * travel_n) / (fall_mag * travel_mag)
        results.append(v if cos_theta >= _COS_FACE_THRESHOLD else None)
    return results
