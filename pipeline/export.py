"""UI JSON export, GeoJSON map export, lift export, and static chart."""

import json
import math
import os

import numpy as np
import matplotlib.pyplot as plt

from .constants import (
    SMOOTH_LEVELS, UI_DATA_DIR,
    GEO_SEGMENT_STEP, TRAVERSE_DELTA_THRESHOLD, FACE_DISPLAY_CAP,
    FACE_SMOOTH_WINDOW_M, STEEPEST_WINDOW_M, SAMPLE_SPACING_M,
)
from .cache import load_json_cache
from .profile import get_steepest, face_steepest_30m, haversine
from .osm import stitch_lifts

# ── Plotting constants ────────────────────────────────────────────────────────

RUN_H_IN = 0.9
SEP_H_IN = 0.30

TIERS = [
    (36, "#333333", "-."),
    (27, "#2196F3", "--"),
    (18, "#4CAF50", ":"),
]


# ── Tier helpers ──────────────────────────────────────────────────────────────

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
            out.append(None)
        out.append(run)
        prev_tier = t
    return out


# ── Static chart ──────────────────────────────────────────────────────────────

def build_figure(all_results: dict, resorts: list) -> plt.Figure:
    resort_names = [r["name"] for r in resorts]
    colors       = {r["name"]: r["color"] for r in resorts}

    col_rows = {rname: _sorted_with_separators(all_results[rname])
                for rname in resort_names}

    if all(len(v) == 0 for v in col_rows.values()):
        raise ValueError("No valid runs to plot.")

    all_dists = [d[-1] for rows in col_rows.values()
                 for item in rows if item is not None
                 for _, d, *_ in [item]]
    x_max = max(all_dists) / 1000 * 1.05

    TITLE_H  = 0.45
    XLABEL_H = 0.35
    L_LABEL  = 2.2
    R_LABEL  = 2.5
    CHART_W  = 6.5
    R_MARGIN = 0.3
    FIG_W    = L_LABEL + CHART_W + R_LABEL + CHART_W + R_MARGIN

    def col_h(rows):
        return sum(RUN_H_IN if r is not None else SEP_H_IN for r in rows)

    fig_h = max(col_h(rows) for rows in col_rows.values()) + TITLE_H + XLABEL_H

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
        y_cur = fig_h - TITLE_H

        first_ax = None
        run_axes = []

        for item in rows:
            row_h = RUN_H_IN if item is not None else SEP_H_IN
            y_bot = y_cur - row_h
            rect  = [cx / FIG_W, y_bot / fig_h, CHART_W / FIG_W, row_h / fig_h]

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


# ── UI JSON export ────────────────────────────────────────────────────────────

def export_for_ui(all_results_by_smooth: dict, resorts: list) -> None:
    """Write per-resort, per-smooth-level JSON files for the Next.js UI."""
    os.makedirs(UI_DATA_DIR, exist_ok=True)

    osm_id_by_resort         = {}
    osm_difficulty_by_resort = {}
    is_area_by_resort        = {}
    for resort in resorts:
        osm_runs = load_json_cache(resort["name"]) or []
        osm_id_by_resort[resort["name"]]         = {r["name"]: r["id"] for r in osm_runs}
        osm_difficulty_by_resort[resort["name"]] = {r["name"]: r.get("osm_difficulty") for r in osm_runs}
        is_area_by_resort[resort["name"]]        = {
            r["name"]: len(r["coords"]) > 2 and r["coords"][0] == r["coords"][-1]
            for r in osm_runs
        }

    index = [
        {"name": r["name"], "slug": r["name"].lower().replace(" ", "_"),
         "color": r["color"], "smooth_levels": SMOOTH_LEVELS,
         "default_bearing": r.get("default_bearing", 180)}
        for r in resorts
    ]
    with open(os.path.join(UI_DATA_DIR, "index.json"), "w") as f:
        json.dump(index, f)

    for resort in resorts:
        name   = resort["name"]
        color  = resort["color"]
        slug   = name.lower().replace(" ", "_")
        osm_id_by_name         = osm_id_by_resort.get(name, {})
        osm_difficulty_by_name = osm_difficulty_by_resort.get(name, {})
        is_area_by_name        = is_area_by_resort.get(name, {})

        if not any(name in results for results in all_results_by_smooth.values()):
            continue

        for smooth_pts, all_results in all_results_by_smooth.items():
            if name not in all_results:
                continue
            rows     = _sorted_with_separators(all_results[name])
            runs_out = []

            for item in rows:
                if item is None:
                    runs_out.append(None)
                    continue
                run_name, dist_m, slope_deg, steepest_override, face_steep = item
                steepest = get_steepest(slope_deg, steepest_override)

                n    = len(dist_m)
                step = max(1, n // 150)
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
                if is_area_by_name.get(run_name):
                    run_entry["is_area"] = True
                runs_out.append(run_entry)

            out_path = os.path.join(UI_DATA_DIR, f"{slug}_s{smooth_pts}.json")
            with open(out_path, "w") as f:
                json.dump({"name": name, "color": color, "runs": runs_out}, f)
            print(f"  UI data → {out_path}  ({len([r for r in runs_out if r])} runs)")


# ── Lift GeoJSON export ───────────────────────────────────────────────────────

def export_lifts_geo_json(resort: dict, lifts: list) -> None:
    slug     = resort["name"].lower().replace(" ", "_")
    path     = os.path.join(UI_DATA_DIR, f"{slug}_lifts.json")
    lifts    = stitch_lifts(lifts)
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


# ── Run GeoJSON export ────────────────────────────────────────────────────────

def export_geo_json(resort: dict, runs_meta: list, raw_samples: list,
                    profiles_s3: list, spacing_m: int = SAMPLE_SPACING_M) -> None:
    """Write a slope-coloured GeoJSON file for the resort map view."""
    from collections import defaultdict as _dd

    slug = resort["name"].lower().replace(" ", "_")
    path = os.path.join(UI_DATA_DIR, f"{slug}_geo.json")

    steepest_queue   = _dd(list)
    face_steep_queue = _dd(list)
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

    def _pop(q, name): return q.get(name, []).pop(0) if q.get(name) else None

    features = []

    for run_name, pts_10m, elevs_10m, _dp, face_slp in raw_samples:
        steepest       = _pop(steepest_queue,   run_name) or 0.0
        _pop(face_steep_queue, run_name)
        osm_id         = _pop(osm_id_queue,         run_name)
        osm_difficulty = _pop(osm_difficulty_queue,  run_name)
        coords_orig    = _pop(orig_coords_queue,     run_name) or []

        face_steep  = face_steepest_30m(face_slp, spacing_m) if face_slp is not None else None
        face_delta  = (face_steep - steepest) if face_steep is not None else None
        is_traverse = face_delta is not None and face_delta >= TRAVERSE_DELTA_THRESHOLD

        if len(coords_orig) > 2 and coords_orig[0] == coords_orig[-1]:
            ring  = [[round(lon, 6), round(lat, 6)] for lat, lon in coords_orig]
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

        face_list = face_slp if face_slp is not None else [None] * len(pts_10m)
        valid     = [(p, e, f) for p, e, f in zip(pts_10m, elevs_10m, face_list)
                     if e is not None]
        if len(valid) < 5:
            continue
        pts_v     = tuple(x[0] for x in valid)
        elevs_arr = np.array([x[1] for x in valid], dtype=float)
        face_v    = [x[2] for x in valid]

        if elevs_arr[0] < elevs_arr[-1]:
            pts_v     = pts_v[::-1]
            elevs_arr = elevs_arr[::-1]
            face_v    = face_v[::-1]

        geo_w  = max(3, 30 // spacing_m)
        kernel = np.ones(geo_w) / geo_w
        half   = geo_w // 2
        elev_s = np.convolve(elevs_arr, kernel, mode="valid")
        pts_s  = pts_v[half: half + len(elev_s)]
        if len(pts_s) < 2:
            continue

        slopes_seg = []
        for i in range(len(pts_s) - 1):
            d  = haversine(*pts_s[i], *pts_s[i + 1])
            if d == 0:
                slopes_seg.append(0.0)
                continue
            dh  = float(elev_s[i] - elev_s[i + 1])
            deg = math.degrees(math.atan2(abs(dh), d)) * (1 if dh >= 0 else -1)
            slopes_seg.append(max(-5.0, min(55.0, deg)))

        slopes_arr    = np.array(slopes_seg)
        slope_w       = max(2, STEEPEST_WINDOW_M // spacing_m)
        slope_kern    = np.ones(slope_w) / slope_w
        slopes_smooth = (np.convolve(slopes_arr, slope_kern, mode="same")
                         if len(slopes_arr) >= slope_w else slopes_arr)

        face_raw  = np.array([f if f is not None else 0.0 for f in face_v], dtype=float)
        face_raw  = np.clip(face_raw, 0.0, 55.0)
        face_w    = max(2, FACE_SMOOTH_WINDOW_M // spacing_m)
        face_kern = np.ones(face_w) / face_w
        face_raw  = (np.convolve(face_raw, face_kern, mode="same")
                     if len(face_raw) >= face_w else face_raw)
        face_trim = face_raw[half: half + len(slopes_smooth)]
        face_trim = np.minimum(face_trim, slopes_smooth + FACE_DISPLAY_CAP)

        use_face      = face_slp is not None and len(face_trim) == len(slopes_smooth)
        display_slopes = np.maximum(face_trim, slopes_smooth) if use_face else slopes_smooth

        step    = max(1, GEO_SEGMENT_STEP * SAMPLE_SPACING_M // spacing_m)
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
            "slopes":         seg_slopes,
            "line_slopes":    seg_line_slopes,
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
        "type":     "FeatureCollection",
        "resort":   resort["name"],
        "color":    resort["color"],
        "features": features,
    }
    with open(path, "w") as f:
        json.dump(geojson, f)
    print(f"  Geo JSON → {path}  ({len(features)} runs)")
