"""Shared pipeline constants — tuning knobs and external URLs."""

CACHE_DIR                = "cache"
SAMPLE_SPACING_M         = 2        # metres between interpolated points along each run
STEEPEST_WINDOW_M        = 10       # rolling-mean window for steepest-pitch metric
FACE_SMOOTH_WINDOW_M     = 10       # rolling-mean window for face steepest (metres)
SMOOTH_LEVELS            = [2, 10, 30]   # exported smoothing windows for the web UI
SMOOTH_POINTS            = 30       # default level used for the static PNG
GEO_SEGMENT_STEP         = 15       # take every Nth 2m point → ~30m map segments
TRAVERSE_DELTA_THRESHOLD = 5.0      # flag as traverse if face_delta >= this (degrees)
FACE_DISPLAY_CAP         = 8.0      # geo display: face can exceed line by at most this
UI_DATA_DIR              = "ui/public/data"

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

USGS_WCS_URL  = (
    "https://elevation.nationalmap.gov/arcgis/services/"
    "3DEPElevation/ImageServer/WCSServer"
)
COPERNICUS_S3    = "https://copernicus-dem-30m.s3.amazonaws.com"
GSI_DEM_URL      = "https://cyberjapandata.gsi.go.jp/xyz"
SWISSTOPO_STAC   = (
    "https://data.geo.admin.ch/api/stac/v0.9/collections/"
    "ch.swisstopo.swissalti3d/items"
)
SPOTLIO_BASE     = "https://autogen.3dmap.spotlio.com"
MIN_SPOTLIO_PTS  = 5    # discard Spotlio runs with fewer than this many coordinate points
