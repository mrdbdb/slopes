# Ski Run Comparison

A tool for comparing ski runs across mountains using objective slope data. Addresses the problem that difficulty ratings (green/blue/black) mean different things at different resorts — a blue at Northstar may ski nothing like a blue at Palisades Tahoe.

## What It Does

Fetches run geometry from OpenStreetMap and elevation from USGS 3DEP, computes a slope profile for every named downhill run at each resort, and presents them in an interactive side-by-side visualization sorted by difficulty.

Two steepness metrics are computed for each run:

- **Line steepness** — directional slope along the path the skier travels (elevation drop ÷ horizontal distance). Follows the [SteepSeeker](https://steepseeker.com) methodology: max rolling 10m-window mean of the per-segment slope. Sample spacing matches the DEM resolution (2m for US resorts, 30m for Canadian resorts).
- **Face steepness** — Horn gradient magnitude (terrain steepness underfoot, independent of travel direction), direction-filtered to exclude slopes beside rather than beneath the skier. Requires the terrain fall line to be within 25° of travel direction. Smoothed over a 10m rolling window to require that steep terrain persists across a patch rather than a single DEM pixel. Used to detect traverses (runs where the face is significantly steeper than the line) and to color map segments more accurately than the line slope alone.

The difficulty tier is based on the steeper of the two metrics:



| Pitch | Difficulty |
|-------|------------|
| < 18° | Beginner |
| 18–27° | Intermediate |
| 27–36° | Advanced |
| 36–47° | Expert |
| 47°+ | Extreme |

## How to Use

### 1. Install dependencies

```bash
pip install requests numpy matplotlib rasterio
cd ui && npm install
```

### 2. Run the data pipeline

```bash
python3 slopesdb_pipeline.py
```

On first run this downloads two assets per resort and caches them locally:
- **DEM tile** — USGS 3DEP 1/3 arc-second (~2m native resolution, resampled to 2m) GeoTIFF via WCS for US resorts; Copernicus GLO-30 (30m) for Canadian resorts (`cache/<Resort>.tif`)
- **OSM run geometry** — all named `piste:type=downhill` ways via Overpass API (`cache/<Resort>.json`)

Same-name OSM ways whose endpoints are within 50m are automatically stitched into a single run before profiling (e.g. a run mapped as separate upper/lower segments becomes one continuous profile).

Slope profiles are computed at three smoothing levels and cached separately (`cache/<Resort>_profiles_s{2,10,30}.json`). Subsequent runs are instant unless you delete a cache file.

Outputs:
- `runs_comparison.png` — static small-multiples chart
- `ui/public/data/*_s{2,10,30}.json` — data files for the web UI (one set per smoothing level)
- `ui/public/data/*_geo.json` — map data with per-segment face and line steepness

### 3. Run the web UI

```bash
cd ui && npm run dev
```

Open [http://localhost:3001](http://localhost:3001).

**Profile chart features:**
- **Difficulty filters** — multi-select checkboxes; hide any combination of tiers (e.g. everything except Expert)
- **Max length** — truncate all runs at a given distance; charts and x-axis scale to the cutoff
- **Smoothing** — switch between 2m (raw), 10m, and 30m/SteepSeeker elevation smoothing
- Runs with the same steepest degree align side-by-side across both columns
- X-axis is consistent across all charts — longer runs visually occupy more width
- Hover to highlight a run; click to pin the highlight
- Each chart shows slope (°) vs distance from top, with reference lines at tier boundaries
- Click a run name to open it on OpenStreetMap (topo layer)
- Filter and smoothing settings are persisted in `localStorage`

**Map view features:**
- Each run is color-coded by steepness tier (green/blue/black/double-black) per 30m segment
- **Face / Line toggle** — switch between face steepness (terrain gradient underfoot) and line steepness (directional slope along the path)
- **Traverse delta slider** — filter to show only runs where face steepness exceeds line steepness by at least N°; highlights runs that cross steep terrain diagonally

### 4. Validate against SteepSeeker

```bash
pip install beautifulsoup4
python3 validate_steepseeker.py
```

Scrapes the SteepSeeker interactive map page for each resort (run data is embedded as a GeoJSON blob in the page HTML), matches runs by name, and prints a comparison table with mean error, MAE, and percentage within ±3°/±5°.

### Refreshing data

```bash
rm cache/Northstar.json cache/Northstar_profiles_s*.json
rm ui/public/data/northstar_geo.json
python3 slopesdb_pipeline.py

rm -rf cache/ ui/public/data/*_geo.json
python3 slopesdb_pipeline.py
```

Note: geo JSON files (`*_geo.json`) are cached separately from profiles and must be deleted explicitly to regenerate map data.

---

## Data Pipeline

```
OpenStreetMap (Overpass API)
  └─ named piste:type=downhill ways
        │
        ▼
  stitch same-name ways with touching endpoints (≤50m gap)
        │
        ▼
  interpolate run path at DEM-resolution spacing
    (2m for US resorts using USGS 3DEP; 30m for Canadian resorts using Copernicus GLO-30)
        │
        ▼
DEM (USGS 3DEP 2m or Copernicus GLO-30 30m, cached GeoTIFF)
  └─ sample elevation at each interpolated point
        │
        ▼
  smooth elevation (2m / 10m / 30m window)
  compute line slope (°) at each segment
  clip to ±55° (removes bad DEM cells / OSM nodes)
        │
        ├─► line steepness: max rolling-mean slope over 10m (or 2 samples minimum)
        │
        ▼
  Horn gradient raster (face steepness = max terrain slope in any direction)
  sample at each interpolated point along run
  direction filter: discard points where terrain fall line is
    >25° from travel direction (excludes slopes beside rather than underfoot)
  smooth over rolling window (requires steep patch to persist,
    not just a single noisy DEM pixel)
        │
        └─► face steepness: max rolling-mean of direction-filtered values
        │
        ▼
  traverse detection: face_steepest − line_steepest ≥ 5° → is_traverse flag
        │
        ├─► cache/  (JSON profiles, one file per smoothing level)
        ├─► runs_comparison.png
        └─► ui/public/data/  (web UI: profiles × smooth level + geo map data)
```

### Face vs line steepness

**Line steepness** measures how quickly you lose elevation along the path you actually ski — it's what a trail map grade represents. On a straight descent it matches the terrain slope; on a traverse it can be much lower than the terrain around you.

**Face steepness** measures the steepness of the terrain surface underfoot using the Horn gradient (the maximum slope in any direction at that point). It's direction-filtered: a point only contributes if the terrain's fall line is within 25° of the skier's travel direction, so terrain to the side is excluded. The result is smoothed over a rolling window (at least 2 samples) so that isolated DEM noise pixels don't spike the reading.

For the map display, face slopes are smoothed over a 10m rolling window and then capped at `line_slope + 8°` per point before the per-segment peak is taken. The smoothing eliminates single-pixel DEM noise; the cap prevents traverse artifacts (face >> line) from coloring segments incorrectly, while leaving genuine steep sections unaffected (on a true steep descent, face and line track closely so the cap is never active).

**Traverse detection** — when face steepness exceeds line steepness by ≥5°, the run is flagged as `is_traverse`. This identifies runs like Village Run or East Creek that cross a steep face diagonally: the terrain is genuinely steep but the skier's path is angled across it. For traverses, `effectiveSteepest` uses the line value so the run is classified by how hard it actually skis rather than the face it crosses.

**Effective steepness** — the value used for tier classification and UI display is `max(line_steepest, face_steepest)` for normal runs, and `line_steepest` for traverses. It is also floored by the OSM difficulty tag (e.g. a run tagged `advanced` is never shown below 27°), which prevents DEM resolution limits or poor OSM geometry from silently under-classifying runs like glades or cliff bands.

## Accuracy

Validation against SteepSeeker (which uses the same OSM + USGS sources):

| Resort | Matched runs | Median error | Within ±3° | Within ±5° |
|--------|-------------|--------------|------------|------------|
| Palisades Tahoe | ~100 | ~2.4° | ~49% | ~66% |
| Northstar | ~52 | ~3.3° | ~44% | ~60% |

Remaining discrepancies come from:
- **DEM out-of-bounds** — run endpoints near the DEM tile edge can return 0.0 (no-data); these are now filtered as invalid elevations
- **OSM coverage gaps** — some runs exist in SteepSeeker's data but are not mapped or correctly tagged in OSM
- **DEM resolution** — Copernicus GLO-30 at 30m may not capture short steep rolls that a finer dataset would; USGS 3DEP at 2m is generally accurate for US resorts

## Resorts

Currently configured: **Palisades Tahoe**, **Northstar**, **Sugar Bowl**, **Mount Norquay**, **Sunshine Village**, **Lake Louise**, and **Whistler Blackcomb**.

To add a resort, add an entry to `RESORTS` in `slopesdb_pipeline.py`:

US resort (USGS 3DEP 2m DEM):
```python
{
    "name":             "Mammoth Mountain",
    "osm_bbox":         "(37.61,-119.04,37.66,-119.00)",
    "dem_bbox":         (-119.04, 37.61, -119.00, 37.66),
    "color":            "darkorange",
    "dem_resolution_m": 2,
}
```

Canadian resort (Copernicus GLO-30 30m DEM):
```python
{
    "name":             "Revelstoke",
    "osm_bbox":         "(50.94,-118.18,51.02,-118.07)",
    "dem_bbox":         (-118.18, 50.94, -118.07, 51.02),
    "color":            "slategray",
    "dem_source":       "copernicus",
    "dem_resolution_m": 30,
}
```

The UI reads the resort list from `ui/public/data/index.json`, which is generated by the pipeline — no UI code changes needed.
