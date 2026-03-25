#!/usr/bin/env python3
"""
SlopesDB pipeline — fetch, process, and export ski run slope data for all configured resorts.

For each resort:
  - Downloads run geometry from OpenStreetMap (Overpass API)
  - Downloads a DEM (USGS 3DEP 2m for US; Copernicus GLO-30 30m for Canada;
    swissALTI3D 2m for Switzerland)
  - Stitches same-name OSM ways with touching endpoints into single runs
  - Computes slope profiles at multiple smoothing levels (2m / 10m / 30m)
  - Computes face steepness (Horn gradient, direction-filtered) and line steepness
  - Exports per-resort JSON for the web UI and a static summary chart

Requirements:
    pip install requests numpy matplotlib rasterio
"""

import os

from pipeline.constants import SMOOTH_LEVELS, SMOOTH_POINTS, UI_DATA_DIR
from pipeline.cache import load_profiles, save_profiles, dem_path_for
from pipeline.dem import (
    download_dem, download_dem_copernicus, download_dem_swisstopo, download_dem_gsi,
    compute_face_slope_raster, sample_face_slopes,
)
from pipeline.osm import fetch_runs, stitch_runs, fetch_spotlio_supplement, fetch_lifts
from pipeline.profile import (
    interpolate_run, profile_area, sample_dem,
    slope_profile, face_steepest_30m,
)
from pipeline.export import export_for_ui, export_geo_json, export_lifts_geo_json, build_figure

import matplotlib.pyplot as plt

# ── Resort configuration ───────────────────────────────────────────────────────

RESORTS = [
    {
        "name":             "Palisades Tahoe",
        "osm_bbox":         "(39.15,-120.30,39.27,-120.17)",
        "dem_bbox":         (-120.30, 39.15, -120.17, 39.27),
        "color":            "steelblue",
        "dem_resolution_m": 2,
    },
    {
        "name":             "Northstar",
        "osm_bbox":         "(39.23,-120.16,39.30,-120.09)",
        "dem_bbox":         (-120.16, 39.23, -120.09, 39.30),
        "color":            "forestgreen",
        "dem_resolution_m": 2,
    },
    {
        "name":             "Sugar Bowl",
        "osm_bbox":         "(39.28,-120.38,39.33,-120.32)",
        "dem_bbox":         (-120.38, 39.28, -120.32, 39.33),
        "color":            "darkorange",
        "dem_resolution_m": 2,
    },
    {
        "name":             "Mount Norquay",
        "osm_bbox":         "(51.19,-115.63,51.23,-115.56)",
        "dem_bbox":         (-115.63, 51.19, -115.56, 51.23),
        "color":            "royalblue",
        "dem_source":       "copernicus",
        "dem_resolution_m": 30,
        "spotlio_uuid":     "54e0b321fbb08c7c6a51abd24bb5ea158d5c3eb479a189662507fab8e5238836",
    },
    {
        "name":             "Sunshine Village",
        "osm_bbox":         "(51.05,-115.82,51.12,-115.73)",
        "dem_bbox":         (-115.82, 51.05, -115.73, 51.12),
        "color":            "goldenrod",
        "dem_source":       "copernicus",
        "dem_resolution_m": 30,
        "spotlio_uuid":     "c15dc51e0e08ee96c8e192afb2b7c04b073c3d37682dd7d8f8bd319fd76221d5",
    },
    {
        "name":             "Lake Louise",
        "osm_bbox":         "(51.40,-116.22,51.47,-116.09)",
        "dem_bbox":         (-116.22, 51.40, -116.09, 51.47),
        "color":            "mediumorchid",
        "dem_source":       "copernicus",
        "dem_resolution_m": 30,
        "spotlio_uuid":     "1cfc07ddc36438c51a0d0a9c9a4c7fe92f6558c8b2094e35d8c4d1c250e6d2a1",
    },
    {
        "name":             "Whistler Blackcomb",
        "osm_bbox":         "(50.04,-123.00,50.15,-122.85)",
        "dem_bbox":         (-123.00, 50.04, -122.85, 50.15),
        "color":            "teal",
        "dem_source":       "copernicus",
        "dem_resolution_m": 30,
    },
    {
        "name":             "Laax",
        "osm_bbox":         "(46.79,9.08,46.90,9.30)",
        "dem_bbox":         (9.08, 46.79, 9.30, 46.90),
        "color":            "crimson",
        "dem_source":       "swisstopo",
        "dem_resolution_m": 2,
        "default_bearing":  0,
    },
    {
        "name":             "Niseko United",
        "osm_bbox":         "(42.77,140.63,42.88,140.82)",
        "dem_bbox":         (140.63, 42.77, 140.82, 42.88),
        "color":            "deepskyblue",
        "dem_source":       "gsi",
        "dem_resolution_m": 5,
        "default_bearing":  0,
    },
    {
        "name":             "Hakuba Valley",
        "osm_bbox":         "(36.61,137.81,36.78,137.93)",
        "dem_bbox":         (137.81, 36.61, 137.93, 36.78),
        "color":            "mediumslateblue",
        "dem_source":       "gsi",
        "dem_resolution_m": 5,
        "default_bearing":  0,
    },
    {
        "name":             "Gala Yuzawa",
        "osm_bbox":         "(36.90,138.79,36.96,138.87)",
        "dem_bbox":         (138.79, 36.90, 138.87, 36.96),
        "color":            "mediumseagreen",
        "dem_source":       "gsi",
        "dem_resolution_m": 5,
        "default_bearing":  0,
    },
    {
        "name":             "Shiga Kogen",
        "osm_bbox":         "(36.68,138.38,36.83,138.60)",
        "dem_bbox":         (138.38, 36.68, 138.60, 36.83),
        "color":            "darkorchid",
        "dem_source":       "gsi",
        "dem_resolution_m": 5,
        "default_bearing":  0,
    },
]


# ── Main ──────────────────────────────────────────────────────────────────────

def _download_dem(resort: dict, tif: str) -> None:
    source = resort.get("dem_source")
    bbox   = resort["dem_bbox"]
    res_m  = resort["dem_resolution_m"]
    if source == "copernicus":
        download_dem_copernicus(bbox, tif, res_m)
    elif source == "swisstopo":
        download_dem_swisstopo(bbox, tif, res_m)
    elif source == "gsi":
        download_dem_gsi(bbox, tif, res_m)
    else:
        download_dem(bbox, tif, res_m)


def _sample_raw(runs: list, tif: str, spacing_m: int):
    """Sample DEM and face slopes for every run; return raw_samples list."""
    face_arr, dz_dx_arr, dz_dy_arr, face_transform = compute_face_slope_raster(tif)
    raw_samples = []
    for run in runs:
        c       = run["coords"]
        is_area = len(c) > 2 and c[0] == c[-1]
        if is_area:
            pts, elevs, _ = profile_area(c[:-1], tif, spacing_m)
            dp_steepest = None
            if pts is None:
                pts   = interpolate_run(c, spacing_m)
                elevs = sample_dem(tif, pts)
        else:
            pts         = interpolate_run(c, spacing_m)
            elevs       = sample_dem(tif, pts)
            dp_steepest = None
        face_slp = (sample_face_slopes(face_arr, dz_dx_arr, dz_dy_arr,
                                       face_transform, pts)
                    if pts is not None else None)
        raw_samples.append((run["name"], pts, elevs, dp_steepest, face_slp))
    return raw_samples


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

        # 1. DEM
        tif = dem_path_for(name, spacing_m)
        if not os.path.exists(tif):
            _download_dem(resort, tif)
        else:
            print(f"  Using cached DEM: {tif}")

        # 2. Runs from OSM (+ optional Spotlio supplement)
        print("  Fetching runs from OSM …", flush=True)
        runs = fetch_runs(name, resort["osm_bbox"])
        print(f"  {len(runs)} ways found")
        runs = stitch_runs(runs)
        print(f"  {len(runs)} runs after stitching")

        if resort.get("spotlio_uuid"):
            extra = fetch_spotlio_supplement(name, resort["spotlio_uuid"], runs)
            runs  = runs + extra

        # 3. Slope profiles (per smoothing level)
        raw_samples = None

        for s in SMOOTH_LEVELS:
            results = load_profiles(name, s)
            if results is not None:
                print(f"  Using cached profiles s={s} ({len(results)} runs)")
            else:
                if raw_samples is None:
                    print("  Sampling DEM …", flush=True)
                    raw_samples = _sample_raw(runs, tif, spacing_m)
                    face_steep_by_run = {
                        run_name: face_steepest_30m(face_slp, spacing_m)
                                  if face_slp is not None else None
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
        geo_out = os.path.join(UI_DATA_DIR,
                               f"{name.lower().replace(' ', '_')}_geo.json")
        if not os.path.exists(geo_out):
            if raw_samples is None:
                print("  Sampling DEM for geo export …", flush=True)
                raw_samples = _sample_raw(runs, tif, spacing_m)
            export_geo_json(resort, runs, raw_samples,
                            all_results_by_smooth[30].get(name, []), spacing_m)
        else:
            print(f"  Geo JSON cached: {geo_out}")

        # 5. Lift data
        lifts_out = os.path.join(UI_DATA_DIR,
                                 f"{name.lower().replace(' ', '_')}_lifts.json")
        if not os.path.exists(lifts_out):
            print("  Fetching lift data from OSM …", flush=True)
            lifts = fetch_lifts(name, resort["osm_bbox"])
            print(f"  {len(lifts)} named lifts found")
            export_lifts_geo_json(resort, lifts)
        else:
            print(f"  Lifts cached: {lifts_out}")

    # 6. UI JSON
    export_for_ui(all_results_by_smooth, RESORTS)

    # 7. Static chart (only when exactly 2 resorts)
    if len(resorts) != 2:
        return
    fig = build_figure(all_results_by_smooth[SMOOTH_POINTS], resorts)
    out = "blue_runs_comparison.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"\n✓ Saved → {out}")
    plt.close(fig)


if __name__ == "__main__":
    main()
