"""OSM data fetching, Spotlio supplement, and run/lift stitching."""

import time
from collections import defaultdict

import requests

from .constants import OVERPASS_URLS, SPOTLIO_BASE, MIN_SPOTLIO_PTS
from .cache import load_json_cache, save_json_cache
from .profile import haversine

STITCH_THRESHOLD_M = 50   # connect endpoints closer than this


# ── OSM fetching ──────────────────────────────────────────────────────────────

def _overpass_fetch(bbox: str) -> dict:
    """Raw Overpass fetch with mirror fallback."""
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
            continue
        coords = [nodes[nid] for nid in e["nodes"] if nid in nodes]
        if len(coords) >= 3:
            runs.append({
                "id":             e["id"],
                "name":           name,
                "osm_difficulty": tags.get("piste:difficulty", "unknown"),
                "coords":         coords,
            })

    save_json_cache(resort_name, runs)
    return runs


# ── Spotlio supplement ────────────────────────────────────────────────────────

def _norm_name(s: str) -> str:
    return s.lower().replace("'", "").replace("\u2019", "").replace("-", " ").strip()


def fetch_spotlio_supplement(resort_name: str, uuid: str, osm_runs: list[dict]) -> list[dict]:
    """Fetch runs from Spotlio that are missing from osm_runs."""
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
    items    = resp.json().get("data", [])
    osm_norm = {_norm_name(r["name"]) for r in osm_runs}

    supplement     = []
    skipped_sparse = 0
    for item in items:
        if not (isinstance(item.get("type"), dict) and item["type"].get("name") == "slope"):
            continue
        name   = item.get("name", "").strip()
        coords = item.get("map_coordinates") or []
        if _norm_name(name) in osm_norm:
            continue
        if len(coords) < MIN_SPOTLIO_PTS:
            skipped_sparse += 1
            continue
        run_coords = [(lat, lon) for lon, lat in coords]
        supplement.append({
            "id":             f"spotlio:{item['uuid']}",
            "name":           name,
            "osm_difficulty": "unknown",
            "coords":         run_coords,
        })

    if skipped_sparse:
        print(f"    skipped {skipped_sparse} Spotlio runs with <{MIN_SPOTLIO_PTS} points")
    print(f"    {len(supplement)} Spotlio runs added")
    save_json_cache(cache_key, supplement)
    return supplement


# ── GPX supplement ───────────────────────────────────────────────────────────

def load_gpx_supplement(resort_name: str, osm_runs: list[dict]) -> list[dict]:
    """Load GPX-derived runs that are missing from osm_runs."""
    import json, os
    path = os.path.join("data", f"{resort_name.replace(' ', '_')}_gpx.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        items = json.load(f)
    osm_norm = {_norm_name(r["name"]) for r in osm_runs}
    supplement = [r for r in items if _norm_name(r["name"]) not in osm_norm]
    for r in supplement:
        r["coords"] = [tuple(c) for c in r["coords"]]
    if supplement:
        print(f"  GPX supplement: {len(supplement)} runs added")
    return supplement


# ── Run stitching ─────────────────────────────────────────────────────────────

def _min_endpoint_dist(w1: dict, w2: dict) -> float:
    c1, c2 = w1["coords"], w2["coords"]
    return min(
        haversine(*c1[ 0], *c2[ 0]),
        haversine(*c1[ 0], *c2[-1]),
        haversine(*c1[-1], *c2[ 0]),
        haversine(*c1[-1], *c2[-1]),
    )


def _try_chain(ways: list[dict]) -> list[tuple] | None:
    """Try to arrange *ways* into a single head-to-tail chain.
    Returns a list of (way, forward:bool) pairs, or None if no valid chain exists.
    """
    def ep(w, forward): return w["coords"][0] if forward else w["coords"][-1]

    def is_free(w, forward):
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

    for start_way in ways:
        rest = [w for w in ways if w is not start_way]
        for fwd in (True, False):
            if is_free(start_way, fwd):
                chain = dfs([(start_way, fwd)], rest, ep(start_way, not fwd))
                if chain is not None:
                    return chain

    for start_way in ways:
        rest  = [w for w in ways if w is not start_way]
        chain = dfs([(start_way, True)], rest, ep(start_way, False))
        if chain is not None:
            return chain

    return None


def stitch_runs(runs: list[dict]) -> list[dict]:
    """Merge same-name OSM ways whose endpoints are within STITCH_THRESHOLD_M."""
    by_name: dict[str, list] = defaultdict(list)
    for r in runs:
        by_name[r["name"]].append(r)

    out = []
    for name, group in by_name.items():
        if len(group) == 1:
            out.append(group[0])
            continue

        n   = len(group)
        adj = [[_min_endpoint_dist(group[i], group[j]) <= STITCH_THRESHOLD_M
                for j in range(n)] for i in range(n)]

        visited    = [False] * n
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


# ── Lift fetching ─────────────────────────────────────────────────────────────

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
    data     = None
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
    """Merge same-name lift OSM ways into a single LineString."""
    by_name: dict[str, list] = defaultdict(list)
    for lift in lifts:
        by_name[lift["name"]].append(lift)

    out = []
    for name, group in by_name.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        chain = _try_chain(group)
        if chain is None:
            out.append(max(group, key=lambda g: len(g["coords"])))
            continue
        merged: list = []
        for i, (w, fwd) in enumerate(chain):
            coords = w["coords"] if fwd else list(reversed(w["coords"]))
            merged.extend(coords if i == 0 else coords[1:])
        print(f"    stitched {len(group)}× lift '{name}'")
        out.append({"name": name, "type": group[0]["type"], "coords": merged})
    return out
