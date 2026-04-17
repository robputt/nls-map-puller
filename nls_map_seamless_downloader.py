#!/usr/bin/env python3
"""
NLS Seamless Historic Map Tile Downloader
------------------------------------------
Downloads XYZ map tiles from the National Library of Scotland seamless
georeferenced layers (maps.nls.uk/geo/explore) for a given bounding box,
and optionally assembles them into a single image.

Usage:
    # List available layers
    python3 nls_map_seamless_downloader.py --list-layers

    # Download OS 6-inch tiles for a bounding box around Plymouth
    python3 nls_map_seamless_downloader.py \\
        --tl-lat 50.42 --tl-lon -4.18 \\
        --br-lat 50.32 --br-lon -4.02 \\
        --layer os_6inch --zoom 14

    # Download and assemble into a single image
    python3 nls_map_seamless_downloader.py \\
        --tl-lat 55.98 --tl-lon -3.25 \\
        --br-lat 55.90 --br-lon -3.10 \\
        --layer os_1inch_7th --zoom 13 --assemble

Attribution: Maps © National Library of Scotland, CC BY 4.0
"""

import argparse
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Layer catalogue  (extracted from maps.nls.uk/geo/scripts/explore-layers-ol6.js)
# tile_url uses standard XYZ {z}/{x}/{y} placeholders
# Some NLS layers use {-y} (TMS y-flip) — handled automatically below
# ---------------------------------------------------------------------------
LAYERS = {
    # ── OS large-scale ──────────────────────────────────────────────────────
    "os_6inch": {
        "title": "OS Six Inch, 1888–1913 (Great Britain)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/os/6inchsecond/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 16,
    },
    "os_25000": {
        "title": "OS 1:25,000 Provisional, 1937–61 (Great Britain)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/os/25000/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 16,
    },
    "os_25000_outline": {
        "title": "OS 1:25,000 Outline, 1945–65 (Great Britain)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/os/25000_outline/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 16,
    },
    # ── OS one-inch ─────────────────────────────────────────────────────────
    "os_1inch_7th": {
        "title": "OS One Inch 7th Series, 1955–61 (England & Wales)",
        "tile_url": "https://api.maptiler.com/tiles/uk-osgb63k1955/{z}/{x}/{y}.jpg?key=7Y0Q1ck46BnB8cXXXg8X",
        "min_zoom": 1, "max_zoom": 15,
    },
    "os_1inch_hills": {
        "title": "OS One Inch, 1885–1903 Hills edition (Great Britain)",
        "tile_url": "https://api.maptiler.com/tiles/uk-osgb63k1885/{z}/{x}/{y}.png?key=7Y0Q1ck46BnB8cXXXg8X",
        "min_zoom": 1, "max_zoom": 15,
    },
    "os_1inch_outline_1885": {
        "title": "OS One Inch Outline, 1885–1900 (Great Britain)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/1inch_2nd_ed/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 15,
    },
    "os_1inch_ireland_outline": {
        "title": "OS One Inch Outline, 1898–1902 (Ireland)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/os/ireland_1inch_2nd_outline/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 15,
    },
    # ── OS 1920s–1940s multi-scale ───────────────────────────────────────────
    "os_1920s_1940s": {
        "title": "OS 1:1m to 1:63K, 1920s–1940s (Great Britain)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/api/nls/{z}/{x}/{y}.jpg",
        "min_zoom": 1, "max_zoom": 14,
    },
    # ── OS 1900s multi-scale ─────────────────────────────────────────────────
    "os_1900s": {
        "title": "OS 1:1m to 1:10K, 1900s (Great Britain)",
        "tile_url": "https://api.maptiler.com/tiles/uk-osgb1888/{z}/{x}/{y}.jpg?key=7Y0Q1ck46BnB8cXXXg8X",
        "min_zoom": 1, "max_zoom": 16,
    },
    # ── OS air photos ────────────────────────────────────────────────────────
    "os_air_photos": {
        "title": "OS Air Photos 1:10,560, 1944–1950 (Great Britain)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/os/air_photos/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 16,
    },
    # ── OS 10-mile thematic ──────────────────────────────────────────────────
    "os_10mile_general": {
        "title": "OS 10 Mile General, 1955 (Great Britain)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/os/ten_mile/general/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 12,
    },
    "os_10mile_roads_1946": {
        "title": "OS 10 Mile Roads, 1946 (Great Britain)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/os/ten_mile/roads_1946/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 12,
    },
    "os_10mile_roads_1956": {
        "title": "OS 10 Mile Roads, 1956 (Great Britain)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/os/ten_mile/roads_1956/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 12,
    },
    "os_10mile_railways": {
        "title": "OS 10 Mile Railways, 1946 (Great Britain)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/os/ten_mile/railways/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 12,
    },
    "os_10mile_admin": {
        "title": "OS 10 Mile Admin Areas, 1956 (Great Britain)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/os/ten_mile/admin/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 12,
    },
    # ── Bartholomew ──────────────────────────────────────────────────────────
    "bartholomew_half_1897": {
        "title": "Bartholomew Half Inch, 1897–1907 (Great Britain)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/bartholomew_great_britain/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 15,
    },
    "bartholomew_half_1940s": {
        "title": "Bartholomew Half Inch, 1940–1947 (Great Britain)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/bartholomew/great_britain_1940s/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 14,
    },
    "bartholomew_half_england_1920s": {
        "title": "Bartholomew Half Inch, 1919–26 (England & Wales)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/bartholomew_england_wales_1920s/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 15,
    },
    "bartholomew_survey_atlas": {
        "title": "Bartholomew Survey Atlas, 1912 (Scotland)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/bartholomew_survey_atlas/{z}/{x}/{-y}.png",
        "min_zoom": 1, "max_zoom": 14,
        "tms": True,
    },
    "bartholomew_half_scotland_1926": {
        "title": "Bartholomew Half Inch, 1926–1935 (Scotland)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/bartholomew/half/{z}/{x}/{-y}.png",
        "min_zoom": 1, "max_zoom": 14,
        "tms": True,
    },
    # ── GSGS ─────────────────────────────────────────────────────────────────
    "gsgs_3906_scotland": {
        "title": "GSGS 3906 1:25,000, 1941 (Scotland)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/os/gsgs3906/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 16,
    },
    "gsgs_london": {
        "title": "GSGS 2157 1:12,500, 1941 (London)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/os/london-12500/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 16,
    },
    # ── County maps ──────────────────────────────────────────────────────────
    "county_cheshire_1794": {
        "title": "Cheshire County Map, 1794",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/county/chester_1794/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 14,
    },
    "county_yorkshire_1828": {
        "title": "Yorkshire County Map, 1828",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/county/yorkshire_1827/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 14,
    },
    "county_lancashire_1830": {
        "title": "Lancashire County Map, 1830",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/county/lancashire_1828/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 14,
    },
    # ── International ────────────────────────────────────────────────────────
    "jamaica": {
        "title": "Jamaica (Bartholomew)",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/jamaica/{z}/{x}/{-y}.png",
        "min_zoom": 1, "max_zoom": 14,
        "tms": True,
    },
    "cyprus": {
        "title": "Cyprus",
        "tile_url": "https://mapseries-tilesets.s3.amazonaws.com/cyprus/{z}/{x}/{y}.png",
        "min_zoom": 1, "max_zoom": 14,
    },
}

HEADERS = {"User-Agent": "NLSSeamlessDownloader/1.0 (educational/research use)"}

# ---------------------------------------------------------------------------
# Back-off state (shared across tile downloads)
# ---------------------------------------------------------------------------
_consecutive_errors = 0
_backoff_until = 0.0
_BASE_DELAY = 0.03


def _throttle():
    remaining = _backoff_until - time.monotonic()
    if remaining > 0:
        print(f"\n  [backoff] waiting {remaining:.1f}s …", end="\r", flush=True)
        time.sleep(remaining)


# ---------------------------------------------------------------------------
# Slippy-map tile math  (Web Mercator / EPSG:3857)
# ---------------------------------------------------------------------------

def _deg2tile(lat_deg: float, lon_deg: float, zoom: int) -> tuple[int, int]:
    """Convert lat/lon to XYZ tile coordinates at a given zoom."""
    lat_r = math.radians(lat_deg)
    n = 2 ** zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def _tile2deg(x: int, y: int, zoom: int) -> tuple[float, float]:
    """Return the NW corner lat/lon of a tile."""
    n = 2 ** zoom
    lon = x / n * 360.0 - 180.0
    lat_r = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
    lat = math.degrees(lat_r)
    return lat, lon


def tiles_for_bbox(tl_lat: float, tl_lon: float,
                   br_lat: float, br_lon: float,
                   zoom: int) -> tuple[range, range]:
    """Return (x_range, y_range) of tile indices covering the bounding box."""
    x_min, y_min = _deg2tile(tl_lat, tl_lon, zoom)
    x_max, y_max = _deg2tile(br_lat, br_lon, zoom)
    # y increases downward, so tl gives smaller y
    return range(x_min, x_max + 1), range(y_min, y_max + 1)


# ---------------------------------------------------------------------------
# Tile download
# ---------------------------------------------------------------------------

def _build_url(template: str, z: int, x: int, y: int, tms: bool = False) -> str:
    ty = (2 ** z - 1 - y) if tms else y
    return template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(ty)).replace("{-y}", str(2 ** z - 1 - y))


def download_tile(url: str, out_path: Path, retries: int = 8) -> bool:
    global _consecutive_errors, _backoff_until

    req = urllib.request.Request(url, headers=HEADERS)

    for attempt in range(retries):
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out_path.write_bytes(r.read())
            _consecutive_errors = 0
            return True

        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                _consecutive_errors += 1
                wait = min(5 * 2 ** attempt, 300)
                _backoff_until = time.monotonic() + wait
                print(f"\n  [HTTP {e.code}] attempt {attempt+1}/{retries} — backing off {wait}s", flush=True)
                time.sleep(wait)
            else:
                # 404 = tile doesn't exist for this area (normal at edges)
                _consecutive_errors = 0
                return False

        except (urllib.error.URLError, TimeoutError, OSError) as e:
            _consecutive_errors += 1
            wait = min(5 * 2 ** attempt, 300)
            _backoff_until = time.monotonic() + wait
            print(f"\n  [network error] attempt {attempt+1}/{retries}: {e} — backing off {wait}s", flush=True)
            time.sleep(wait)

    print(f"\n  [WARN] Gave up on tile after {retries} attempts: {url}", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# Main download logic
# ---------------------------------------------------------------------------

def download_tiles(layer_key: str, tl_lat: float, tl_lon: float,
                   br_lat: float, br_lon: float, zoom: int,
                   out_dir: Path) -> dict:
    """Download all tiles for the bbox. Returns {(x,y): path} for found tiles."""
    layer = LAYERS[layer_key]
    tms = layer.get("tms", False)
    template = layer["tile_url"]

    x_range, y_range = tiles_for_bbox(tl_lat, tl_lon, br_lat, br_lon, zoom)
    total = len(x_range) * len(y_range)

    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"  Layer : {layer['title']}")
    print(f"  Zoom  : {zoom}  |  Tiles: {len(x_range)} × {len(y_range)} = {total}")
    print(f"  Output: {out_dir}")

    downloaded = {}
    done = 0

    for y in y_range:
        for x in x_range:
            tile_path = out_dir / f"{zoom}_{x}_{y}.png"
            url = _build_url(template, zoom, x, y, tms)

            if tile_path.exists():
                downloaded[(x, y)] = tile_path
                done += 1
            else:
                if download_tile(url, tile_path):
                    downloaded[(x, y)] = tile_path
                    done += 1
                # 404s are normal at coastlines/edges — don't count as errors

            print(f"  [{done}/{total}] z{zoom}/{x}/{y}", end="\r", flush=True)

            pause = _BASE_DELAY * (2 ** min(_consecutive_errors, 4))
            time.sleep(pause)

    print(f"\n  Downloaded {done}/{total} tiles")
    return downloaded


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def assemble(downloaded: dict, x_range: range, y_range: range,
             zoom: int, out_path: Path) -> bool:
    try:
        from PIL import Image
    except ImportError:
        print("[INFO] Pillow not installed — skipping assembly. pip install Pillow")
        return False

    if not downloaded:
        print("[WARN] No tiles to assemble.")
        return False

    # Probe tile size from first tile
    first = next(iter(downloaded.values()))
    with Image.open(first) as probe:
        tw, th = probe.size

    cols = len(x_range)
    rows = len(y_range)
    canvas = Image.new("RGBA", (cols * tw, rows * th), (255, 255, 255, 0))

    for row_i, y in enumerate(y_range):
        for col_i, x in enumerate(x_range):
            path = downloaded.get((x, y))
            if path and path.exists():
                with Image.open(path) as tile:
                    canvas.paste(tile.convert("RGBA"), (col_i * tw, row_i * th))

    # Save as PNG to preserve transparency, or JPEG if fully opaque
    canvas.convert("RGB").save(out_path, "JPEG", quality=92)
    print(f"  Assembled → {out_path}  ({cols * tw} × {rows * th} px)")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Download NLS seamless historic map tiles for a bounding box",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--tl-lat", type=float, help="Top-left latitude")
    p.add_argument("--tl-lon", type=float, help="Top-left longitude")
    p.add_argument("--br-lat", type=float, help="Bottom-right latitude")
    p.add_argument("--br-lon", type=float, help="Bottom-right longitude")
    p.add_argument("--layer", default="os_6inch",
                   help="Layer key (default: os_6inch). Use --list-layers to see all.")
    p.add_argument("--zoom", type=int, default=14,
                   help="Zoom level (default: 14). Higher = more detail, many more tiles.")
    p.add_argument("--out-dir", default="nls_seamless",
                   help="Output directory (default: nls_seamless)")
    p.add_argument("--assemble", action="store_true",
                   help="Stitch all tiles into a single image (requires Pillow)")
    p.add_argument("--list-layers", action="store_true",
                   help="List all available layer keys and exit")
    return p.parse_args()


def main():
    args = parse_args()

    if args.list_layers:
        print(f"\n{'Key':<40} {'Zoom':<10} Title")
        print("-" * 100)
        for key, meta in LAYERS.items():
            zrange = f"z{meta['min_zoom']}–{meta['max_zoom']}"
            print(f"{key:<40} {zrange:<10} {meta['title']}")
        print()
        return

    if not all([args.tl_lat, args.tl_lon, args.br_lat, args.br_lon]):
        print("[ERROR] --tl-lat, --tl-lon, --br-lat, --br-lon are all required.", file=sys.stderr)
        sys.exit(1)

    if args.layer not in LAYERS:
        print(f"[ERROR] Unknown layer '{args.layer}'. Run --list-layers to see options.", file=sys.stderr)
        sys.exit(1)

    layer = LAYERS[args.layer]
    zoom = args.zoom
    if not (layer["min_zoom"] <= zoom <= layer["max_zoom"]):
        print(f"[WARN] Zoom {zoom} is outside this layer's supported range "
              f"z{layer['min_zoom']}–{layer['max_zoom']}.")

    x_range, y_range = tiles_for_bbox(args.tl_lat, args.tl_lon, args.br_lat, args.br_lon, zoom)
    total = len(x_range) * len(y_range)
    if total > 1000:
        print(f"[WARN] This will download {total} tiles. Consider using a lower zoom level.")
        try:
            ans = input("Continue? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans != "y":
            print("Aborted.")
            return

    out_dir = Path(args.out_dir) / args.layer / str(zoom)

    print(f"\nDownloading '{layer['title']}' at zoom {zoom}")
    downloaded = download_tiles(
        layer_key=args.layer,
        tl_lat=args.tl_lat, tl_lon=args.tl_lon,
        br_lat=args.br_lat, br_lon=args.br_lon,
        zoom=zoom,
        out_dir=out_dir,
    )

    if args.assemble and downloaded:
        out_img = Path(args.out_dir) / f"{args.layer}_z{zoom}.jpg"
        assemble(downloaded, x_range, y_range, zoom, out_img)

    print(f"\nDone. Files saved to: {Path(args.out_dir).resolve()}")


if __name__ == "__main__":
    main()
