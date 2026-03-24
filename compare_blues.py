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
]

SAMPLE_SPACING_M = 10       # meters between sample points along each run
SMOOTH_POINTS    = 3        # default; used for the static PNG only
SMOOTH_LEVELS    = [1, 2, 3]  # exported for the web UI (1 = raw, 3 = SteepSeeker)
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

    index = []
    for resort in RESORTS:
        name  = resort["name"]
        color = resort["color"]
        slug  = name.lower().replace(" ", "_")
        osm_id_by_name = osm_id_by_resort[name]

        for smooth_pts, all_results in all_results_by_smooth.items():
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

        index.append({"name": name, "slug": slug, "color": color,
                      "smooth_levels": SMOOTH_LEVELS})

    with open(os.path.join(UI_DATA_DIR, "index.json"), "w") as f:
        json.dump(index, f)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    all_results_by_smooth: dict[int, dict] = {s: {} for s in SMOOTH_LEVELS}

    for resort in RESORTS:
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

    # 4. Export JSON for the web UI
    export_for_ui(all_results_by_smooth)

    # 5. Plot (use default smooth level)
    fig = build_figure(all_results_by_smooth[SMOOTH_POINTS])

    out = "blue_runs_comparison.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"\n✓ Saved → {out}")
    plt.close(fig)


if __name__ == "__main__":
    main()
