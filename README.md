# NLS Historic Map Tools

A set of Python scripts for downloading and querying out-of-copyright historic
maps from the [National Library of Scotland](https://maps.nls.uk).

No API key required. All maps are CC-BY licensed.

---

## Scripts

| Script | What it does |
|--------|-------------|
| `nls_map_downloader.py` | Downloads individual paper map sheets (IIIF tiles) for a lat/lon point |
| `nls_map_seamless_downloader.py` | Downloads seamless XYZ mosaic tiles for a bounding box |
| `nls_map_geocoder.py` | OCRs downloaded tiles to build a reverse-geocoding index |

---

## Requirements

Python 3.10+. Install dependencies into a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install Pillow pytesseract
```

For the geocoder, Tesseract must also be installed on your system:

```bash
brew install tesseract        # macOS
sudo apt install tesseract-ocr  # Debian/Ubuntu
```

---

## `nls_map_downloader.py` — Individual map sheets

Finds historic map sheets covering a single location, filters to out-of-copyright
maps (>50 years old), and downloads them as IIIF image tiles.

**How it works:**
1. Queries the NLS GeoServer WFS API for sheets covering your lat/lon
2. Filters to maps published more than 50 years ago
3. Fetches IIIF metadata and downloads tiles from `map-view.nls.uk`

```bash
# List all available map series
python3 nls_map_downloader.py --list-layers

# List matching sheets without downloading
python3 nls_map_downloader.py --lat 55.9533 --lon -3.1883 --layer OS_6inch_all_find --list-only

# Download at quarter resolution (good default)
python3 nls_map_downloader.py --lat 55.9533 --lon -3.1883 --layer OS_6inch_all_find

# Download and stitch into a single image
python3 nls_map_downloader.py --lat 51.5074 --lon -0.1278 --layer OS_25inch_all_find --assemble

# Full resolution
python3 nls_map_downloader.py --lat 55.9533 --lon -3.1883 --layer OS_6inch_all_find --scale-factor 1

# Restrict to a year range
python3 nls_map_downloader.py --lat 55.9533 --lon -3.1883 --layer OS_6inch_all_find --year-min 1880 --year-max 1920
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--lat` | required | Latitude (WGS84 decimal degrees) |
| `--lon` | required | Longitude (WGS84 decimal degrees) |
| `--layer` | `OS_6inch_all_find` | WFS layer name (see `--list-layers`) |
| `--year-min` | 1 | Earliest publication year |
| `--year-max` | current year − 50 | Latest publication year (OOC cutoff) |
| `--max-maps` | 10 | Max sheets to download |
| `--scale-factor` | 4 | IIIF scale: 1=full res, 2=half, 4=quarter, 8=eighth |
| `--tile-size` | 512 | IIIF tile size in pixels |
| `--out-dir` | `nls_maps` | Output directory |
| `--assemble` | off | Stitch tiles into one image (needs Pillow) |
| `--list-layers` | — | Print available layers and exit |
| `--list-only` | — | List matching maps without downloading |

**Output:**

```
nls_maps/
  1852_Edinburghshire_Sheet_2_74426700/
    tile_000_000.jpg
    tile_000_001.jpg
    ...
  1852_Edinburghshire_Sheet_2_74426700_assembled.jpg  ← if --assemble
```

---

## `nls_map_seamless_downloader.py` — Seamless mosaic tiles

Downloads standard XYZ map tiles from the NLS seamless georeferenced layers
(the same layers shown at [maps.nls.uk/geo/explore](https://maps.nls.uk/geo/explore))
for a given bounding box. Tiles are saved in a format compatible with the geocoder.

```bash
# List all available seamless layers
python3 nls_map_seamless_downloader.py --list-layers

# Download OS 6-inch tiles for a bounding box (Plymouth area)
python3 nls_map_seamless_downloader.py \
  --tl-lat 50.42 --tl-lon -4.18 \
  --br-lat 50.32 --br-lon -4.02 \
  --layer os_6inch --zoom 14

# Download and assemble into a single image
python3 nls_map_seamless_downloader.py \
  --tl-lat 55.98 --tl-lon -3.25 \
  --br-lat 55.90 --br-lon -3.10 \
  --layer os_1inch_7th --zoom 13 --assemble
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--tl-lat` | required | Top-left latitude |
| `--tl-lon` | required | Top-left longitude |
| `--br-lat` | required | Bottom-right latitude |
| `--br-lon` | required | Bottom-right longitude |
| `--layer` | `os_6inch` | Layer key (see `--list-layers`) |
| `--zoom` | 14 | Zoom level — higher means more detail and more tiles |
| `--out-dir` | `nls_seamless` | Output directory |
| `--assemble` | off | Stitch tiles into one image (needs Pillow) |
| `--list-layers` | — | Print available layers and exit |

**Zoom level guide:**

| Zoom | Approx scale | Tiles for ~10km² |
|------|-------------|-----------------|
| 12 | 1:150,000 | ~4 |
| 13 | 1:75,000 | ~12 |
| 14 | 1:37,000 | ~40 |
| 15 | 1:18,000 | ~150 |
| 16 | 1:9,000 | ~600 |

The script warns you before downloading more than 1,000 tiles.

**Output:**

```
nls_seamless/
  os_6inch/
    14/
      14_8002_5528.png
      14_8003_5528.png
      ...
  os_6inch_z14.jpg  ← if --assemble
```

---

## `nls_map_geocoder.py` — Historic map reverse geocoder

OCRs downloaded seamless tiles to extract place name labels, stores them in a
SQLite database with their geographic coordinates, and lets you query what the
historic map called any given location.

> Best results at zoom 15–16 where map text is large enough for reliable OCR.
> Zoom 14 works but yields fewer labels per tile.

### Step 1 — Download tiles

```bash
python3 nls_map_seamless_downloader.py \
  --tl-lat 50.42 --tl-lon -4.18 \
  --br-lat 50.32 --br-lon -4.02 \
  --layer os_6inch --zoom 15
```

### Step 2 — Build the index

```bash
python3 nls_map_geocoder.py index --tiles nls_seamless/os_6inch/15
```

This OCRs every tile and stores extracted labels in `nls_geocoder.db`. Re-runs
skip nothing (use a fresh `--db` path if you want to re-index).

### Step 3 — Query

```bash
# What did the historic map call this location?
python3 nls_map_geocoder.py query --lat 50.3653 --lon -4.0845

# Wider search radius
python3 nls_map_geocoder.py query --lat 50.3653 --lon -4.0845 --radius 1000

# Return more results
python3 nls_map_geocoder.py query --lat 50.3653 --lon -4.0845 --radius 2000 --limit 20
```

**Example output:**
```
Historic map labels near (50.36530, -4.08450)  [within 300m]

    Distance  Label                                  Lat          Lon
  --------------------------------------------------------------------
         87m  Plympton                          50.36612     -4.08321
        142m  St-Mary                           50.36489     -4.08109
        201m  Ridgeway                          50.36801     -4.08534
```

**Index options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--tiles` | required | Directory of downloaded tiles |
| `--db` | `nls_geocoder.db` | SQLite database path |

**Query options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--lat` | required | Latitude to query |
| `--lon` | required | Longitude to query |
| `--radius` | 300 | Search radius in metres |
| `--limit` | 10 | Max results to return |
| `--db` | `nls_geocoder.db` | SQLite database path |

---

## Attribution

Maps are © National Library of Scotland, licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
You must credit NLS when using or publishing these maps.
