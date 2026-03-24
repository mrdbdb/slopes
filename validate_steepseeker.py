#!/usr/bin/env python3
"""
Fetch SteepSeeker run data for a resort and compare steepest-section pitch
against our locally-computed profiles.

Requirements:
    pip install requests beautifulsoup4

Usage:
    python3 validate_steepseeker.py
"""

import re
import ast
import json
import math
import requests

# ── Config ────────────────────────────────────────────────────────────────────

RESORTS = [
    {
        "name":     "Palisades Tahoe",
        "ss_url":   "https://steepseeker.com/interactive-map/CA/Palisades%20Tahoe",
        "profiles": "cache/Palisades_Tahoe_profiles.json",
    },
    {
        "name":     "Northstar",
        "ss_url":   "https://steepseeker.com/interactive-map/CA/Northstar",
        "profiles": "cache/Northstar_profiles.json",
    },
]

HEADERS = {"User-Agent": "Mozilla/5.0 (research/validation script)"}

# ── Fetch SteepSeeker GeoJSON ─────────────────────────────────────────────────

def fetch_steepseeker(url: str) -> list[dict]:
    """
    Pull the inline `let trails = {...}` GeoJSON from a SteepSeeker
    interactive-map page and return a list of run dicts:
        {name, steepest_30m, pitches: {30:°, 50:°, 100:°, 200:°, 500:°, 1000:°}}
    """
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    # Find the start of the inline GeoJSON blob
    marker = re.search(r"let trails\s*=\s*(\{)", resp.text)
    if not marker:
        raise ValueError(f"Could not find 'let trails' in {url}")

    # Walk forward counting braces to find the matching closing brace
    start  = marker.start(1)
    depth  = 0
    end    = start
    for i, ch in enumerate(resp.text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    else:
        raise ValueError("Could not find closing brace for trails GeoJSON")

    geojson = ast.literal_eval(resp.text[start:end])
    runs = []
    for feat in geojson.get("features", []):
        props = feat.get("properties", {})
        name  = props.get("label", "").strip()
        popup = props.get("popupContent", "")
        if not name or not popup:
            continue

        # Parse pitch values from popup HTML
        pitches = {}
        for window in (30, 50, 100, 200, 500, 1000):
            pm = re.search(rf"{window}m Pitch:?\s*([\d.]+)°", popup)
            if pm:
                pitches[window] = float(pm.group(1))

        if 30 not in pitches:
            continue   # lift or incomplete entry

        runs.append({
            "name":         name,
            "steepest_30m": pitches[30],
            "pitches":      pitches,
            "color":        props.get("color", ""),
        })

    return runs

# ── Load our profiles ─────────────────────────────────────────────────────────

def load_our_profiles(path: str) -> dict[str, float]:
    """Return {run_name: steepest_deg} from a profiles cache file."""
    import numpy as np
    with open(path) as f:
        raw = json.load(f)
    out = {}
    for r in raw:
        if r["slope_deg"] is None:
            continue
        out[r["name"]] = float(np.max(r["slope_deg"]))
    return out

# ── Fuzzy name matching ───────────────────────────────────────────────────────

def _normalise(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())

def match_name(ss_name: str, our_names: list[str]) -> str | None:
    """Find the closest name in our dataset for a SteepSeeker run name."""
    sn = _normalise(ss_name)
    for n in our_names:
        if _normalise(n) == sn:
            return n
    # Partial match — SteepSeeker name is contained in ours or vice-versa
    for n in our_names:
        on = _normalise(n)
        if sn in on or on in sn:
            return n
    return None

# ── Comparison ────────────────────────────────────────────────────────────────

def compare(resort: dict) -> None:
    print(f"\n{'═'*60}")
    print(f"  {resort['name']}")
    print(f"{'═'*60}")

    print("  Fetching SteepSeeker data …", flush=True)
    ss_runs  = fetch_steepseeker(resort["ss_url"])
    our_data = load_our_profiles(resort["profiles"])
    our_names = list(our_data.keys())

    print(f"  SteepSeeker: {len(ss_runs)} runs   Ours: {len(our_names)} runs\n")

    fmt = "  {:<35} {:>8}  {:>8}  {:>8}"
    print(fmt.format("Run", "SS 30m°", "Ours°", "Δ°"))
    print("  " + "-" * 58)

    deltas = []
    unmatched_ss = []

    for ss in sorted(ss_runs, key=lambda x: x["steepest_30m"], reverse=True):
        our_name = match_name(ss["name"], our_names)
        if our_name is None:
            unmatched_ss.append(ss["name"])
            print(fmt.format(ss["name"][:35], f"{ss['steepest_30m']:.1f}", "—", "—"))
            continue
        our_val = our_data[our_name]
        delta   = our_val - ss["steepest_30m"]
        deltas.append(delta)
        flag = "  ◀ large" if abs(delta) > 5 else ""
        print(fmt.format(ss["name"][:35], f"{ss['steepest_30m']:.1f}",
                         f"{our_val:.1f}", f"{delta:+.1f}") + flag)

    if deltas:
        import numpy as np
        d = np.array(deltas)
        print(f"\n  Matched {len(deltas)} runs")
        print(f"  Mean error : {d.mean():+.2f}°")
        print(f"  Median error: {np.median(d):+.2f}°")
        print(f"  MAE        : {np.abs(d).mean():.2f}°")
        print(f"  Within ±3° : {(np.abs(d) <= 3).mean()*100:.0f}%")
        print(f"  Within ±5° : {(np.abs(d) <= 5).mean()*100:.0f}%")

    if unmatched_ss:
        print(f"\n  SteepSeeker runs with no OSM match ({len(unmatched_ss)}):")
        for n in unmatched_ss:
            print(f"    · {n}")


def main():
    for resort in RESORTS:
        compare(resort)


if __name__ == "__main__":
    main()
