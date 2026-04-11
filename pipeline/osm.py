"""OSM data fetching, Spotlio supplement, and run/lift stitching."""

import time
from collections import defaultdict

import requests

from .constants import OVERPASS_URLS, SPOTLIO_BASE, MIN_SPOTLIO_PTS
from .cache import load_json_cache, save_json_cache
from .profile import haversine

STITCH_THRESHOLD_M = 50   # connect endpoints closer than this

# Overpass area id offsets (https://wiki.openstreetmap.org/wiki/Overpass_API#Areas)
AREA_OFFSET_RELATION = 3_600_000_000
AREA_OFFSET_WAY      = 2_400_000_000


# ── Overpass transport ────────────────────────────────────────────────────────

def _overpass_query(query: str, label: str = "overpass", timeout_s: int = 90,
                    max_attempts: int = 2) -> dict:
    """POST *query* to Overpass with mirror fallback and verbose status prints.

    Each mirror is tried up to *max_attempts* times before moving on, with a
    progressive backoff schedule (5s → 15s → 30s) — Overpass mirrors are
    aggressive about rate-limiting and return 504/timeouts when hammered.
    """
    backoff = [5, 15, 30]
    last_err = None
    for attempt in range(max_attempts):
        for url in OVERPASS_URLS:
            try:
                tag = f" (try {attempt+1})" if attempt else ""
                print(f"    {label}: trying {url.split('/')[2]}{tag} …",
                      end=" ", flush=True)
                resp = requests.post(url, data={"data": query}, timeout=timeout_s)
                resp.raise_for_status()
                print("ok")
                return resp.json()
            except Exception as e:
                msg = str(e).split("\n")[0][:80]
                print(f"failed ({msg})")
                last_err = e
                time.sleep(backoff[min(attempt, len(backoff) - 1)])
    raise RuntimeError(f"All Overpass mirrors failed. Last error: {last_err}")


# ── Overpass query builders ───────────────────────────────────────────────────

def _piste_query_bbox(bbox: str) -> str:
    return f"""
[out:json][timeout:120];
(
  way["piste:type"="downhill"]{bbox};
);
out body;
>;
out skel qt;
"""


def _piste_query_area(area_id: int) -> str:
    return f"""
[out:json][timeout:120];
area({area_id})->.a;
(
  way["piste:type"="downhill"](area.a);
);
out body;
>;
out skel qt;
"""


def _piste_query_relation(relation_id: int) -> str:
    return f"""
[out:json][timeout:120];
relation({relation_id});
way(r)["piste:type"="downhill"];
(._;>;);
out body;
"""


# ── Parse Overpass → runs ─────────────────────────────────────────────────────

def _runs_from_overpass_data(data: dict) -> list[dict]:
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
    return runs


# ── Resort area discovery ─────────────────────────────────────────────────────

def resolve_resort_area(name: str, name_regex: str | None = None) -> dict | None:
    """Find the OSM element that represents this resort as a whole.

    Tries, in priority order:
      1. ``relation site=piste``           (OpenSkiMap convention — pistes are explicit members)
      2. ``landuse=winter_sports`` area    (way or relation, name-matched)
      3. ``leisure=resort`` + ``sport=skiing`` area

    Returns a dict::

        {"kind": "relation_site" | "area_landuse" | "area_leisure",
         "id":       <osm element id>,
         "area_id":  <overpass area id or None for relation_site>,
         "name":     <matched OSM name>}

    or ``None`` if nothing matched. Pass *name_regex* to override the default
    name match (useful for resorts with ambiguous or multi-word names).
    """
    regex = (name_regex or name).replace('"', '\\"')
    q = f"""
[out:json][timeout:60];
(
  relation["site"="piste"]["name"~"{regex}",i];
  relation["landuse"="winter_sports"]["name"~"{regex}",i];
  way["landuse"="winter_sports"]["name"~"{regex}",i];
  relation["leisure"="resort"]["sport"~"skiing|ski",i]["name"~"{regex}",i];
);
out tags;
"""
    data = _overpass_query(q, label="resort lookup", timeout_s=60)
    elements = data.get("elements", [])
    if not elements:
        return None

    target = name.lower()

    # Drop polygons whose names suggest non-alpine winter use — XC trail
    # systems and tubing parks are tagged the same way as alpine resorts in
    # OSM and otherwise win the match (e.g. "Crested Butte Nordic" beats the
    # untagged alpine resort polygon). Skip the filter when the user is
    # explicitly looking for a nordic centre.
    NEG_KEYWORDS = (
        "nordic", "cross-country", "cross country", "xc",
        "tubing", "snowshoe", "snow tubing", "snow park",
    )
    target_is_neg = any(k in target for k in NEG_KEYWORDS)
    if not target_is_neg:
        elements = [
            e for e in elements
            if not any(
                k in ((e.get("tags") or {}).get("name") or "").lower()
                for k in NEG_KEYWORDS
            )
        ]
    if not elements:
        return None

    def score(e):
        tags = e.get("tags") or {}
        elem_name = (tags.get("name") or "").lower()
        exact     = (elem_name == target)
        startswith = elem_name.startswith(target)
        if tags.get("site") == "piste":               kind_prio = 3
        elif tags.get("landuse") == "winter_sports":  kind_prio = 2
        else:                                         kind_prio = 1
        # Higher is better. Prefer: exact name, then prefix, then kind, then shorter name.
        return (exact, startswith, kind_prio, -len(elem_name))

    best = max(elements, key=score)
    tags = best.get("tags") or {}
    etype = best["type"]
    eid   = best["id"]

    if tags.get("site") == "piste":
        return {"kind": "relation_site", "id": eid, "area_id": None,
                "name": tags.get("name", name)}

    area_id = eid + (AREA_OFFSET_RELATION if etype == "relation" else AREA_OFFSET_WAY)
    kind = "area_landuse" if tags.get("landuse") == "winter_sports" else "area_leisure"
    return {"kind": kind, "id": eid, "area_id": area_id,
            "name": tags.get("name", name)}


# ── Top-level run fetchers ────────────────────────────────────────────────────

def fetch_runs(resort: dict) -> list[dict]:
    """Fetch downhill pistes for *resort*, trying the most precise strategy first.

    Strategies, in order:
      1. Explicit ``resort["osm_area"]`` override — one of::

             {"kind": "relation",    "id": 12345}   # piste/site relation
             {"kind": "area",        "id": 3600012345}  # overpass area id
             {"kind": "area_way",    "id": 67890}   # raw way id  → area_id = +2.4e9
             {"kind": "area_rel",    "id": 12345}   # raw relation id → area_id = +3.6e9

      2. Automatic discovery by name via :func:`resolve_resort_area`, if no
         ``osm_bbox`` was provided or ``osm_discover`` is truthy.
      3. ``resort["osm_bbox"]`` string fallback (legacy path).

    Results are cached on disk under the resort name, so subsequent calls
    re-use them regardless of which strategy produced them.
    """
    resort_name = resort["name"]
    cached = load_json_cache(resort_name)
    if cached is not None:
        print(f"  Using cached OSM data ({len(cached)} runs)")
        return cached

    runs = _fetch_runs_fresh(resort)
    save_json_cache(resort_name, runs)
    return runs


def _fetch_runs_fresh(resort: dict) -> list[dict]:
    name = resort["name"]

    # 1. Explicit override
    area_ref = resort.get("osm_area")
    if area_ref:
        kind = area_ref.get("kind")
        rid  = area_ref.get("id")
        if kind == "relation":
            print(f"  OSM piste relation #{rid}")
            return _runs_from_overpass_data(
                _overpass_query(_piste_query_relation(rid), label="piste relation"))
        if kind == "area":
            print(f"  OSM area {rid}")
            return _runs_from_overpass_data(
                _overpass_query(_piste_query_area(rid), label="piste area"))
        if kind == "area_rel":
            aid = rid + AREA_OFFSET_RELATION
            print(f"  OSM area (from relation #{rid}) = {aid}")
            return _runs_from_overpass_data(
                _overpass_query(_piste_query_area(aid), label="piste area"))
        if kind == "area_way":
            aid = rid + AREA_OFFSET_WAY
            print(f"  OSM area (from way #{rid}) = {aid}")
            return _runs_from_overpass_data(
                _overpass_query(_piste_query_area(aid), label="piste area"))
        raise ValueError(f"{name}: unknown osm_area kind {kind!r}")

    # 2. Auto-discovery — default when no bbox is supplied
    bbox = resort.get("osm_bbox")
    should_discover = resort.get("osm_discover", bbox is None)
    if should_discover:
        info = resolve_resort_area(name, resort.get("osm_name_regex"))
        if info:
            print(f"  Resolved OSM: {info['kind']} #{info['id']} ({info['name']})")
            if info["kind"] == "relation_site":
                return _runs_from_overpass_data(
                    _overpass_query(_piste_query_relation(info["id"]),
                                    label="piste relation"))
            return _runs_from_overpass_data(
                _overpass_query(_piste_query_area(info["area_id"]),
                                label="piste area"))
        if bbox is None:
            raise RuntimeError(
                f"{name}: OSM discovery found no match and no osm_bbox fallback. "
                f"Pass osm_name_regex, osm_area, or osm_bbox in the resort config.")
        print("  OSM discovery found no match — falling back to osm_bbox")

    # 3. Bbox fallback
    if not bbox:
        raise ValueError(f"{name}: no osm_area, no discovery match, and no osm_bbox")
    return _runs_from_overpass_data(
        _overpass_query(_piste_query_bbox(bbox), label="piste bbox"))


# ── Geometry helpers ──────────────────────────────────────────────────────────

def bbox_from_runs(runs: list[dict], padding_deg: float = 0.005) -> tuple[float, float, float, float]:
    """Compute ``(lon_min, lat_min, lon_max, lat_max)`` from all run coords + padding."""
    lats: list[float] = []
    lons: list[float] = []
    for r in runs:
        for lat, lon in r["coords"]:
            lats.append(lat)
            lons.append(lon)
    if not lats:
        raise ValueError("bbox_from_runs: no coordinates in runs")
    return (min(lons) - padding_deg, min(lats) - padding_deg,
            max(lons) + padding_deg, max(lats) + padding_deg)


def overpass_bbox_string(dem_bbox: tuple[float, float, float, float]) -> str:
    """Convert ``(lon_min, lat_min, lon_max, lat_max)`` → Overpass ``"(lat1,lon1,lat2,lon2)"``."""
    lon1, lat1, lon2, lat2 = dem_bbox
    return f"({lat1},{lon1},{lat2},{lon2})"


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
    data = _overpass_query(query, label="lifts bbox", timeout_s=60)

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
