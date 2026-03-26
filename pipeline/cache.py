"""Local disk cache helpers for OSM data, DEM paths, and slope profiles."""

import os
import json
import numpy as np

from .constants import CACHE_DIR


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
         r.get("face_steepest"))
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


def dem_path_for(name: str, resolution_m: int) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return _cache_path(f"{name}_{resolution_m}m", "tif")


def load_bearing(name: str) -> int | None:
    path = _cache_path(f"{name}_bearing", "json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def save_bearing(name: str, bearing: int) -> None:
    path = _cache_path(f"{name}_bearing", "json")
    with open(path, "w") as f:
        json.dump(bearing, f)
