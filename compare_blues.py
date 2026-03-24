#!/usr/bin/env python3
"""
Compare intermediate (blue) ski runs: Palisades Tahoe vs Northstar California.
Plots slope profile (degrees vs distance) for every blue run at each resort.

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
    },
    {
        "name": "Northstar",
        "osm_bbox": "(39.23,-120.16,39.30,-120.09)",
        "dem_bbox": (-120.16, 39.23, -120.09, 39.30),
        "color": "forestgreen",
    },
    {
        "name": "Sugar Bowl",
        "osm_bbox": "(39.28,-120.38,39.33,-120.32)",
        "dem_bbox": (-120.38, 39.28, -120.32, 39.33),
        "color": "darkorange",
    },
]

SAMPLE_SPACING_M = 10       # meters between sample points along each run
SMOOTH_POINTS    = 3        # default; used for the static PNG only
SMOOTH_LEVELS    = [1, 2, 3]  # exported for the web UI (1 = raw, 3 = SteepSeeker)
GEO_SEGMENT_STEP = 3        # take every Nth 10m point → ~30m map segments
DEM_RESOLUTION_M = 10       # requested DEM pixel size
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
         np.array(r["slope_deg"]) if r["slope_deg"] is not None else None)
        for r in raw
    ]


def save_profiles(name: str, smooth_pts: int, results: list) -> None:
    path = profiles_cache_path(name, smooth_pts)
    serialisable = [
        {"name": run_name,
         "dist_m":    dist.tolist() if dist is not None else None,
         "slope_deg": slope.tolist() if slope is not None else None}
        for run_name, dist, slope in results
    ]
    with open(path, "w") as f:
        json.dump(serialisable, f)
    print(f"  Profiles saved → {path}")


# ── DEM download ──────────────────────────────────────────────────────────────

def dem_path_for(name: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return _cache_path(name, "tif")


def download_dem(bbox_wsen: tuple, output_path: str, resolution_m: int = 10):
    """
    Download a USGS 3DEP GeoTIFF via WCS for the given bounding box.
    bbox_wsen: (west, south, east, north) in WGS-84 degrees.
    """
    west, south, east, north = bbox_wsen
    lat_c = (south + north) / 2
    width_m  = (east - west) * 111_000 * math.cos(math.radians(lat_c))
    height_m = (north - south) * 111_000
    width_px  = max(100, int(width_m  / resolution_m))
    height_px = max(100, int(height_m / resolution_m))

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
    print(f"  Downloading DEM ({width_px}×{height_px} px) …", flush=True)
    resp = requests.get(USGS_WCS_URL, params=params, timeout=120, stream=True)
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)
    print(f"  Saved → {output_path}")


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


def steepest_30m(slope_deg: np.ndarray) -> float:
    """Max of a 3-point (30 m) rolling mean on downhill segments.
    Matches SteepSeeker's 'steepest 30 m' methodology."""
    down = slope_deg[slope_deg > 0]
    if len(down) < 3:
        return float(np.max(down)) if len(down) > 0 else 0.0
    rolling = np.convolve(down, np.ones(3) / 3.0, mode="valid")
    return float(np.max(rolling))

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
    valid = [(n, d, s) for n, d, s in results if s is not None]
    valid.sort(key=lambda x: steepest_30m(x[2]), reverse=True)

    out, prev_tier = [], None
    for run in valid:
        t = _tier(steepest_30m(run[2]))
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
                 for _, d, _ in [item]]
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

            run_name, dist_m, slope_deg = item
            steepest = steepest_30m(slope_deg)

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

    # Build OSM id maps once
    osm_id_by_resort = {}
    for resort in RESORTS:
        osm_runs = load_json_cache(resort["name"]) or []
        osm_id_by_resort[resort["name"]] = {r["name"]: r["id"] for r in osm_runs}

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
        osm_id_by_name = osm_id_by_resort.get(name, {})

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
                run_name, dist_m, slope_deg = item
                steepest = steepest_30m(slope_deg)

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
                osm_id = osm_id_by_name.get(run_name)
                if osm_id is not None:
                    run_entry["osm_id"] = osm_id
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


def export_lifts_geo_json(resort: dict, lifts: list) -> None:
    slug = resort["name"].lower().replace(" ", "_")
    path = os.path.join(UI_DATA_DIR, f"{slug}_lifts.json")
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

def export_geo_json(resort: dict, runs_meta: list, raw_samples: list, profiles_s3: list) -> None:
    """Write a slope-coloured GeoJSON file for the resort map view."""
    slug = resort["name"].lower().replace(" ", "_")
    path = os.path.join(UI_DATA_DIR, f"{slug}_geo.json")

    steepest_by_name = {
        run_name: steepest_30m(slope)
        for run_name, _, slope in profiles_s3
        if slope is not None
    }
    osm_id_by_name = {r["name"]: r["id"] for r in runs_meta}

    step = GEO_SEGMENT_STEP
    features = []

    for run_name, pts_10m, elevs_10m in raw_samples:
        indices = list(range(0, len(pts_10m), step))
        if indices[-1] != len(pts_10m) - 1:
            indices.append(len(pts_10m) - 1)

        pts_s   = [pts_10m[i]   for i in indices]
        elevs_s = [elevs_10m[i] for i in indices]

        valid = [(p, e) for p, e in zip(pts_s, elevs_s) if e is not None]
        if len(valid) < 3:
            continue
        pts_v, elevs_v = zip(*valid)
        elevs_arr = np.array(elevs_v, dtype=float)

        # Orient top-to-bottom
        if elevs_arr[0] < elevs_arr[-1]:
            pts_v     = pts_v[::-1]
            elevs_arr = elevs_arr[::-1]

        seg_slopes = []
        for i in range(len(pts_v) - 1):
            d = haversine(*pts_v[i], *pts_v[i + 1])
            if d == 0:
                seg_slopes.append(0.0)
                continue
            dh = float(elevs_arr[i] - elevs_arr[i + 1])   # positive = downhill
            deg = math.degrees(math.atan2(abs(dh), d)) * (1 if dh >= 0 else -1)
            seg_slopes.append(round(max(-5.0, min(55.0, deg)), 1))

        coords = [[round(lon, 6), round(lat, 6)] for lat, lon in pts_v]

        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "name":     run_name,
                "steepest": round(steepest_by_name.get(run_name, 0.0), 1),
                "osm_id":   osm_id_by_name.get(run_name),
                "slopes":   seg_slopes,
            },
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
        name = resort["name"]
        print(f"\n▶ {name}")

        # 1. DEM (download once, cache locally)
        tif = dem_path_for(name)
        if not os.path.exists(tif):
            download_dem(resort["dem_bbox"], tif, DEM_RESOLUTION_M)
        else:
            print(f"  Using cached DEM: {tif}")

        # 2. Runs from OSM
        print("  Fetching blue runs from OSM …", flush=True)
        runs = fetch_runs(name, resort["osm_bbox"])
        print(f"  {len(runs)} ways found")
        runs = stitch_runs(runs)
        print(f"  {len(runs)} runs after stitching")

        # 3. For each smooth level, load or compute profiles.
        #    DEM is sampled once (lazily) if any level needs computing.
        raw_samples = None   # (pts, elevs) per run — loaded on demand

        for s in SMOOTH_LEVELS:
            results = load_profiles(name, s)
            if results is not None:
                print(f"  Using cached profiles s={s} ({len(results)} runs)")
            else:
                if raw_samples is None:
                    print("  Sampling DEM …", flush=True)
                    raw_samples = []
                    for run in runs:
                        pts   = interpolate_run(run["coords"], SAMPLE_SPACING_M)
                        elevs = sample_dem(tif, pts)
                        raw_samples.append((run["name"], pts, elevs))

                results = []
                for run_name, pts, elevs in raw_samples:
                    dist, slope = slope_profile(pts, elevs, s)
                    results.append((run_name, dist, slope))
                    status = "ok" if dist is not None else "skip"
                    print(f"    s={s}  {run_name[:38]:<38}  [{status}]")
                save_profiles(name, s, results)

            all_results_by_smooth[s][name] = results

        # 4. Geo JSON for map view
        geo_out = os.path.join(UI_DATA_DIR, f"{name.lower().replace(' ', '_')}_geo.json")
        if not os.path.exists(geo_out):
            if raw_samples is None:
                print("  Sampling DEM for geo export …", flush=True)
                raw_samples = []
                for run in runs:
                    pts   = interpolate_run(run["coords"], SAMPLE_SPACING_M)
                    elevs = sample_dem(tif, pts)
                    raw_samples.append((run["name"], pts, elevs))
            export_geo_json(resort, runs, raw_samples,
                            all_results_by_smooth[3].get(name, []))
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

    # 7. Plot (only when all resorts were processed)
    if len(resorts) < len(RESORTS):
        return
    fig = build_figure(all_results_by_smooth[SMOOTH_POINTS])

    out = "blue_runs_comparison.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"\n✓ Saved → {out}")
    plt.close(fig)


if __name__ == "__main__":
    main()
