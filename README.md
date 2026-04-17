# NLS Historic Map Tile Downloader

Downloads out-of-copyright (>50 years old) historic map tiles from the
[National Library of Scotland](https://maps.nls.uk) for any given location.

## How it works

1. Queries the NLS GeoServer WFS API to find map sheets covering your location
2. Filters to only maps published more than 50 years ago (out of copyright)
3. Fetches IIIF image metadata for each sheet from `map-view.nls.uk`
4. Downloads tiles at your chosen resolution and optionally assembles them

No API key required. All maps are CC-BY licensed.

## Requirements

Python 3.10+ (stdlib only). For tile assembly, install Pillow:

```bash
pip install Pillow
```

## Usage

```bash
# List all available map layers
python3 nls_map_downloader.py --list-layers

# List maps covering a location (no download)
python3 nls_map_downloader.py --lat 55.9533 --lon -3.1883 --layer OS_6inch_all_find --list-only

# Download maps (scale-factor 4 = quarter resolution, good balance of size/detail)
python3 nls_map_downloader.py --lat 55.9533 --lon -3.1883 --layer OS_6inch_all_find

# Download and stitch tiles into a single image (requires Pillow)
python3 nls_map_downloader.py --lat 51.5074 --lon -0.1278 --layer OS_25inch_all_find --assemble

# Full resolution (scale-factor 1) — large files
python3 nls_map_downloader.py --lat 55.9533 --lon -3.1883 --layer OS_6inch_all_find --scale-factor 1

# Restrict to a specific year range
python3 nls_map_downloader.py --lat 55.9533 --lon -3.1883 --layer OS_6inch_all_find --year-min 1880 --year-max 1920
```

## Options

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

## Output

Tiles are saved as individual JPEGs in subdirectories named `{year}_{sheet}_{image_id}/`:

```
nls_maps/
  1852_Edinburghshire_Sheet_2_74426700/
    tile_000_000.jpg
    tile_000_001.jpg
    ...
  1852_Edinburghshire_Sheet_2_74426700_assembled.jpg  ← if --assemble
```

## Attribution

Maps are © National Library of Scotland, licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
You must credit NLS when using or publishing these maps.
