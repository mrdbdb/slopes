#!/usr/bin/env python3
"""Probe OSM resort-area discovery for every resort in RESORTS.

Calls ``resolve_resort_area`` against the Overpass API for each configured
resort and reports which OSM element (site=piste relation, landuse polygon,
or leisure=resort polygon) we'd use as the scope for piste fetching. Useful
for deciding which resorts can drop their hand-crafted ``osm_bbox`` in
favour of systematic discovery, and for spotting name collisions.

Usage:

    python3 probe_osm_resorts.py                  # all resorts
    python3 probe_osm_resorts.py Vail Breckenridge  # filter by name

Network access to an Overpass mirror is required.
"""

import sys
import time

from pipeline.osm import resolve_resort_area
from slopesdb_pipeline import RESORTS

# Pause between resorts to avoid hammering Overpass mirrors (which are
# aggressive about rate-limiting).
INTER_RESORT_SLEEP_S = 2.0


def probe(resort: dict) -> tuple[str, str]:
    name  = resort["name"]
    regex = resort.get("osm_name_regex")
    try:
        info = resolve_resort_area(name, regex)
    except Exception as e:
        return ("ERROR", f"{type(e).__name__}: {e}")

    if info is None:
        return ("MISS",
                f"no site=piste / landuse=winter_sports / leisure=resort match"
                + (f"  (regex={regex!r})" if regex else ""))

    detail = f"{info['kind']} #{info['id']}  →  {info['name']!r}"
    if info["area_id"] is not None:
        detail += f"  (area_id={info['area_id']})"
    return ("OK", detail)


def main() -> int:
    wanted = {a.lower() for a in sys.argv[1:]}
    targets = (
        [r for r in RESORTS if r["name"].lower() in wanted]
        if wanted else RESORTS
    )
    if wanted and not targets:
        print(f"No resort matched {sorted(wanted)}", file=sys.stderr)
        return 1

    print(f"Probing {len(targets)} resort(s)…\n")
    counts: dict[str, int] = {"OK": 0, "MISS": 0, "ERROR": 0}
    errored: list[str] = []
    missed:  list[str] = []
    for i, r in enumerate(targets):
        if i > 0:
            time.sleep(INTER_RESORT_SLEEP_S)
        status, detail = probe(r)
        counts[status] += 1
        if status == "ERROR":
            errored.append(r["name"])
        elif status == "MISS":
            missed.append(r["name"])
        print(f"  [{status:5}] {r['name']:<22}  {detail}")

    print()
    print(f"Summary: {counts['OK']} resolved, "
          f"{counts['MISS']} no match, {counts['ERROR']} errored")

    if missed:
        print(f"\nNo OSM area for: {', '.join(missed)}")
        print("  → keep their osm_bbox in RESORTS or add osm_area override")
    if errored:
        print(f"\nNetwork errors for: {', '.join(errored)}")
        print("  → re-run targeting just these once Overpass mirrors recover:")
        quoted = " ".join(f'"{n}"' if " " in n else n for n in errored)
        print(f"     python3 probe_osm_resorts.py {quoted}")
    return 0 if counts["ERROR"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
