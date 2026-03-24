# Ski Run Comparison

A tool for comparing ski runs across mountains using objective slope data. Addresses the problem that difficulty ratings (green/blue/black) mean different things at different resorts — a blue at Northstar may ski nothing like a blue at Palisades Tahoe.

## What It Does

Fetches run geometry from OpenStreetMap and elevation from USGS 3DEP, computes a slope profile for every named downhill run at each resort, and presents them in an interactive side-by-side visualization sorted by difficulty.

The difficulty metric follows the [SteepSeeker](https://steepseeker.com) methodology: the steepest rolling 30-meter pitch along the run, computed from 10-meter elevation samples.

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
python3 compare_blues.py
```

On first run this downloads two assets per resort and caches them locally:
- **DEM tile** — USGS 3DEP 10m-resolution GeoTIFF via WCS (`cache/<Resort>.tif`)
- **OSM run geometry** — all named `piste:type=downhill` ways via Overpass API (`cache/<Resort>.json`)

Same-name OSM ways whose endpoints are within 50m are automatically stitched into a single run before profiling (e.g. a run mapped as separate upper/lower segments becomes one continuous profile).

Slope profiles are computed at three smoothing levels and cached separately (`cache/<Resort>_profiles_s{1,2,3}.json`). Subsequent runs are instant unless you delete a cache file.

Outputs:
- `blue_runs_comparison.png` — static small-multiples chart
- `ui/public/data/*_s{1,2,3}.json` — data files for the web UI (one set per smoothing level)

### 3. Run the web UI

```bash
cd ui && npm run dev
```

Open [http://localhost:3001](http://localhost:3001).

**UI features:**
- **Difficulty filters** — multi-select checkboxes; hide any combination of tiers (e.g. everything except Expert)
- **Max length** — truncate all runs at a given distance; charts and x-axis scale to the cutoff
- **Smoothing** — switch between raw (1), 20m (2), and 30m/SteepSeeker (3) elevation smoothing
- Runs with the same steepest degree align side-by-side across both columns
- X-axis is consistent across all charts — longer runs visually occupy more width
- Hover to highlight a run; click to pin the highlight
- Each chart shows slope (°) vs distance from top, with reference lines at tier boundaries
- Click a run name to open it on OpenStreetMap (topo layer)
- Filter and smoothing settings are persisted in `localStorage`

### 4. Validate against SteepSeeker

```bash
pip install beautifulsoup4
python3 validate_steepseeker.py
```

Scrapes the SteepSeeker interactive map page for each resort (run data is embedded as a GeoJSON blob in the page HTML), matches runs by name, and prints a comparison table with mean error, MAE, and percentage within ±3°/±5°.

### Refreshing data

```bash
# Re-fetch OSM + recompute profiles for one resort
rm cache/Northstar.json cache/Northstar_profiles_s*.json
python3 compare_blues.py

# Re-fetch everything
rm -rf cache/
python3 compare_blues.py   # re-downloads DEMs too (slow)
```

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
  interpolate to 10m points along each run
        │
        ▼
USGS 3DEP (WCS download, cached GeoTIFF)
  └─ sample elevation at each point
        │
        ▼
  smooth elevation (1/2/3-point window — 3 = SteepSeeker)
  compute slope at each segment
  clip to ±55° (removes bad DEM cells / OSM nodes)
        │
        ▼
  steepest 30m = max of 3-point rolling mean on smoothed slopes
  (matches SteepSeeker methodology)
        │
        ├─► cache/  (JSON profiles, one file per smoothing level)
        ├─► blue_runs_comparison.png
        └─► ui/public/data/  (web UI, one file per resort × smooth level)
```

## Accuracy

Validation against SteepSeeker (which uses the same OSM + USGS sources):

| Resort | Matched runs | Median error | Within ±3° | Within ±5° |
|--------|-------------|--------------|------------|------------|
| Palisades Tahoe | ~100 | ~2.4° | ~49% | ~66% |
| Northstar | ~52 | ~3.3° | ~44% | ~60% |

Remaining discrepancies come from:
- **DEM out-of-bounds** — run endpoints near the DEM tile edge can return 0.0 (no-data); these are now filtered as invalid elevations
- **OSM coverage gaps** — some runs exist in SteepSeeker's data but are not mapped or correctly tagged in OSM
- **DEM resolution** — USGS 3DEP at 10m may not capture short steep rolls that a surveyed dataset would

## Resorts

Currently configured: **Palisades Tahoe** and **Northstar California**.

To add a resort, add an entry to `RESORTS` in `compare_blues.py`:

```python
{
    "name":     "Mammoth Mountain",
    "osm_bbox": "(37.61,-119.04,37.66,-119.00)",
    "dem_bbox": (-119.04, 37.61, -119.00, 37.66),
    "color":    "darkorange",
}
```

Then add a fetch entry in `validate_steepseeker.py` and `ui/app/page.tsx`.
