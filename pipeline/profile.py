"""Geometry utilities and slope-profile computation."""

import math
from math import radians, cos, sin, asin, sqrt

import numpy as np

from .constants import SAMPLE_SPACING_M, STEEPEST_WINDOW_M, FACE_SMOOTH_WINDOW_M
from .dem import sample_dem


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


# ── Area profiling ─────────────────────────────────────────────────────────────

def profile_area(coords, dem_path, spacing_m=SAMPLE_SPACING_M):
    """
    For an area run (polygon without the closing duplicate), sample a spacing_m
    grid inside the polygon and follow the steepest-descent path from the highest
    interior point.  Also compute the globally-optimal steepest 30m pitch via DP.

    Returns (pts, elevs, dp_steepest), or (None, None, None) if the area is too small.
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

    lat_steps, v = [], lat_min
    while v <= lat_max + dlat:
        lat_steps.append(v); v += dlat
    lon_steps, v = [], lon_min
    while v <= lon_max + dlon:
        lon_steps.append(v); v += dlon

    grid     = {}
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


def _dp_steepest_30m_area(elev_grid: dict, grid: dict) -> float:
    """Compute steepest_30m for an area by searching over ALL possible 3-step
    downhill paths using dynamic programming (O(8n) time)."""
    DIRS = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]

    if len(elev_grid) < 4:
        return 0.0

    sorted_cells = sorted(elev_grid.keys(), key=lambda k: elev_grid[k], reverse=True)

    best1: dict = {}
    best2: dict = {}
    best3: dict = {}

    for cell in sorted_cells:
        ri, ci  = cell
        e_cell  = elev_grid[cell]
        pt_cell = grid[cell]

        s1_best = s2_best = s3_best = -math.inf

        for dr, dc in DIRS:
            prev = (ri + dr, ci + dc)
            if prev not in elev_grid:
                continue
            e_prev = elev_grid[prev]
            if e_prev <= e_cell:
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


# ── Slope profile ─────────────────────────────────────────────────────────────

def slope_profile(coords, elevs, smooth_pts: int):
    """
    Returns (cum_dist_m, slope_deg) arrays for one run.
    Positive = downhill.
    """
    dists = [0.0]
    for i in range(1, len(coords)):
        dists.append(dists[-1] + haversine(*coords[i - 1], *coords[i]))
    cum = np.array(dists)

    mask = np.array([e is not None for e in elevs])
    if mask.sum() < 4:
        return None, None
    cum_c  = cum[mask]
    elev_c = np.array([e for e in elevs if e is not None], dtype=float)

    if elev_c[0] < elev_c[-1]:
        cum_c  = cum_c[-1] - cum_c[::-1]
        elev_c = elev_c[::-1]

    if smooth_pts > 1:
        if len(elev_c) < smooth_pts:
            return None, None
        kernel = np.ones(smooth_pts) / smooth_pts
        half   = smooth_pts // 2
        elev_c = np.convolve(elev_c, kernel, mode="valid")
        cum_c  = cum_c[half: half + len(elev_c)]
        if len(cum_c) < 2:
            return None, None

    horiz = np.diff(cum_c)
    vert  = -np.diff(elev_c)
    with np.errstate(divide="ignore", invalid="ignore"):
        slope_deg = np.where(
            horiz > 0,
            np.degrees(np.arctan2(np.abs(vert), horiz)) * np.sign(vert),
            0.0,
        )

    seg_mid   = (cum_c[:-1] + cum_c[1:]) / 2
    slope_deg = np.clip(slope_deg, -5.0, 55.0)
    return seg_mid, slope_deg


def steepest_30m(slope_deg: np.ndarray, spacing_m: int = SAMPLE_SPACING_M) -> float:
    """Max rolling-mean slope over STEEPEST_WINDOW_M on downhill segments."""
    w    = max(2, STEEPEST_WINDOW_M // spacing_m)
    down = slope_deg[slope_deg > 0]
    if len(down) < w:
        return float(np.max(down)) if len(down) > 0 else 0.0
    rolling = np.convolve(down, np.ones(w) / w, mode="valid")
    return float(np.max(rolling))


def face_steepest_30m(face_slopes: list, spacing_m: int = SAMPLE_SPACING_M) -> float:
    """Max direction-filtered face slope along the run, smoothed over FACE_SMOOTH_WINDOW_M."""
    valid   = np.array([s if s is not None else 0.0 for s in face_slopes], dtype=float)
    valid   = np.clip(valid, 0.0, 55.0)
    w       = max(2, FACE_SMOOTH_WINDOW_M // spacing_m)
    down    = valid[valid > 0]
    if len(down) < w:
        return float(np.max(down)) if len(down) > 0 else 0.0
    rolling = np.convolve(down, np.ones(w) / w, mode="valid")
    return float(np.max(rolling))


def get_steepest(slope_deg: np.ndarray | None, override: float | None,
                 spacing_m: int = SAMPLE_SPACING_M) -> float:
    """Return the steepest-30m value, preferring an explicit override (area runs)."""
    if override is not None:
        return override
    if slope_deg is None:
        return 0.0
    return steepest_30m(slope_deg, spacing_m)
