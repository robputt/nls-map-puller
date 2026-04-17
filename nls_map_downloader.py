#!/usr/bin/env python3
"""
NLS Historic Map Tile Downloader
---------------------------------
Downloads out-of-copyright (>50 years old) historic map tiles from the
National Library of Scotland (maps.nls.uk) for a given location.

Usage:
    python3 nls_map_downloader.py --lat 55.9533 --lon -3.1883 --layer OS_6inch_all_find
    python3 nls_map_downloader.py --lat 51.5074 --lon -0.1278 --layer OS_25inch_all_find --year-max 1970
    python3 nls_map_downloader.py --lat 55.9533 --lon -3.1883 --list-layers
    python3 nls_map_downloader.py --lat 55.9533 --lon -3.1883 --layer OS_6inch_all_find --tile-size 512 --zoom 4

How it works:
    1. Queries the NLS GeoServer WFS API to find map sheets covering the location.
    2. Filters to only maps published >50 years ago (out of copyright).
    3. Fetches IIIF image metadata for each sheet.
    4. Downloads tiles at the requested zoom/scale factor and assembles them.
"""

import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import date

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GEOSERVER_WFS = "https://geoserver4.nls.uk/geoserver/wfs"
IIIF_BASE = "https://map-view.nls.uk/iiif/2"
COPYRIGHT_CUTOFF_YEAR = date.today().year - 50  # maps published before this year are OOC

# All available WFS layer typenames (from the site's JS source)
AVAILABLE_LAYERS = {
    # Large-scale OS maps
    "OS_25inch_all_find":       "OS 25-inch to the mile (1:2,500) - all editions",
    "OS_6inch_all_find":        "OS 6-inch to the mile (1:10,560) - all editions",
    "OS_25000_uk":              "OS 1:25,000 Provisional edition (1937-1961)",
    "OS_one_inch_combined":     "OS 1-inch to the mile (1:63,360) - combined",
    "OS_National_Grid_all_find":"OS National Grid series (1944-1975)",
    "os_half_inch":             "OS Half-inch to the mile (1:126,720)",
    "os_quarter_inch":          "OS Quarter-inch to the mile (1:253,440)",
    # Bartholomew
    "bart_half_combined":       "Bartholomew Half-inch (1:126,720)",
    # Town plans
    "OS_Town_Plans":            "OS Town Plans",
    "towns":                    "Town plans (various publishers)",
    # Air photos
    "catalog_air_photos":       "Air photographs (1944-1950)",
    # Historical / pre-OS
    "Pont":                     "Timothy Pont maps (c.1583-1614)",
    "Gordon":                   "Robert Gordon maps (1636-1652)",
    "Blaeu_Maps":               "Blaeu Atlas Novus (1654)",
    "Thomson":                  "John Thomson Atlas (1820s)",
    "estate_maps":              "Estate maps",
    # Coastal / marine
    "coastal_charts_large_scale": "Coastal charts - large scale",
    "coastal_charts_small_scale": "Coastal charts - small scale",
    # Combined / multi-series
    "TM_Combined_sorted_27700": "All georeferenced maps (combined)",
}

HEADERS = {"User-Agent": "NLSMapDownloader/1.0 (educational/research use)"}


# ---------------------------------------------------------------------------
# Coordinate conversion: WGS84 -> OSGB36 (EPSG:27700)
# ---------------------------------------------------------------------------
# Helmert transform + Airy ellipsoid -> GRS80 ellipsoid
# Accurate to ~5m, sufficient for WFS point queries.

def latlon_to_osgb(lat_deg: float, lon_deg: float) -> tuple[float, float]:
    """Convert WGS84 lat/lon (degrees) to OSGB36 Easting/Northing (metres)."""
    # --- WGS84 -> OSGB36 Helmert transform ---
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)

    # WGS84 ellipsoid
    a_wgs = 6378137.0
    b_wgs = 6356752.3142
    e2_wgs = 1 - (b_wgs ** 2) / (a_wgs ** 2)

    nu = a_wgs / math.sqrt(1 - e2_wgs * math.sin(lat) ** 2)
    x1 = (nu + 0) * math.cos(lat) * math.cos(lon)
    y1 = (nu + 0) * math.cos(lat) * math.sin(lon)
    z1 = (nu * (1 - e2_wgs) + 0) * math.sin(lat)

    # Helmert parameters (WGS84 -> OSGB36)
    tx, ty, tz = -446.448, 125.157, -542.060   # metres
    rx = math.radians(-0.1502 / 3600)
    ry = math.radians(-0.2470 / 3600)
    rz = math.radians(-0.8421 / 3600)
    s  = 20.4894e-6

    x2 = tx + (1 + s) * (x1 - rz * y1 + ry * z1)
    y2 = ty + (1 + s) * (rz * x1 + y1 - rx * z1)
    z2 = tz + (1 + s) * (-ry * x1 + rx * y1 + z1)

    # Airy 1830 ellipsoid
    a_airy = 6377563.396
    b_airy = 6356256.909
    e2_airy = 1 - (b_airy ** 2) / (a_airy ** 2)

    lon2 = math.atan2(y2, x2)
    p = math.sqrt(x2 ** 2 + y2 ** 2)
    lat2 = math.atan2(z2, p * (1 - e2_airy))
    for _ in range(10):
        nu2 = a_airy / math.sqrt(1 - e2_airy * math.sin(lat2) ** 2)
        lat2 = math.atan2(z2 + e2_airy * nu2 * math.sin(lat2), p)

    # OSGB36 -> National Grid (Transverse Mercator)
    N0, E0 = -100000.0, 400000.0
    phi0 = math.radians(49.0)
    lam0 = math.radians(-2.0)
    F0 = 0.9996012717

    nu3 = a_airy * F0 / math.sqrt(1 - e2_airy * math.sin(lat2) ** 2)
    rho = a_airy * F0 * (1 - e2_airy) / (1 - e2_airy * math.sin(lat2) ** 2) ** 1.5
    eta2 = nu3 / rho - 1

    n = (a_airy - b_airy) / (a_airy + b_airy)
    M = (b_airy * F0 * (
        (1 + n + 5/4 * n**2 + 5/4 * n**3) * (lat2 - phi0)
        - (3*n + 3*n**2 + 21/8 * n**3) * math.sin(lat2 - phi0) * math.cos(lat2 + phi0)
        + (15/8 * n**2 + 15/8 * n**3) * math.sin(2*(lat2 - phi0)) * math.cos(2*(lat2 + phi0))
        - 35/24 * n**3 * math.sin(3*(lat2 - phi0)) * math.cos(3*(lat2 + phi0))
    ))

    I   = M + N0
    II  = nu3 / 2 * math.sin(lat2) * math.cos(lat2)
    III = nu3 / 24 * math.sin(lat2) * math.cos(lat2)**3 * (5 - math.tan(lat2)**2 + 9*eta2)
    IIIA= nu3 / 720 * math.sin(lat2) * math.cos(lat2)**5 * (61 - 58*math.tan(lat2)**2 + math.tan(lat2)**4)
    IV  = nu3 * math.cos(lat2)
    V   = nu3 / 6 * math.cos(lat2)**3 * (nu3/rho - math.tan(lat2)**2)
    VI  = nu3 / 120 * math.cos(lat2)**5 * (5 - 18*math.tan(lat2)**2 + math.tan(lat2)**4 + 14*eta2 - 58*math.tan(lat2)**2*eta2)

    dl = lon2 - lam0
    N = I + II*dl**2 + III*dl**4 + IIIA*dl**6
    E = E0 + IV*dl + V*dl**3 + VI*dl**5

    return E, N


# ---------------------------------------------------------------------------
# WFS query
# ---------------------------------------------------------------------------

def query_maps(lat: float, lon: float, typename: str,
               year_min: int = 1, year_max: int = None,
               max_features: int = 50) -> list[dict]:
    """Query NLS GeoServer WFS for map sheets covering a lat/lon point."""
    if year_max is None:
        year_max = COPYRIGHT_CUTOFF_YEAR

    easting, northing = latlon_to_osgb(lat, lon)
    cql = f"INTERSECTS(the_geom,POINT({easting:.0f} {northing:.0f}))"

    params = {
        "service": "WFS",
        "version": "1.1.0",
        "request": "GetFeature",
        "typename": f"nls:{typename}",
        "PropertyName": "(the_geom,IMAGE,IMAGETHUMB,IMAGEURL,SHEET,DATES,YEAR)",
        "outputFormat": "application/json",
        "srsname": "EPSG:27700",
        "cql_filter": cql,
        "maxFeatures": str(max_features),
    }

    url = GEOSERVER_WFS + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)

    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"[ERROR] WFS request failed: {e.code} {e.reason}", file=sys.stderr)
        return []

    features = data.get("features", [])

    # Filter by year (out-of-copyright only)
    results = []
    for f in features:
        props = f.get("properties", {})
        year_str = props.get("YEAR", "")
        try:
            year = int(str(year_str).strip()[:4])
        except (ValueError, TypeError):
            year = 0
        if year_min <= year <= year_max:
            results.append({
                "image_id": props.get("IMAGE", ""),
                "sheet":    props.get("SHEET", ""),
                "year":     year,
                "dates":    props.get("DATES", ""),
                "thumb":    props.get("IMAGETHUMB", ""),
                "view_url": props.get("IMAGEURL", ""),
            })

    return results


# ---------------------------------------------------------------------------
# IIIF tile download
# ---------------------------------------------------------------------------

def get_iiif_info(image_id: str) -> dict | None:
    """Fetch IIIF Image API info.json for a given NLS image ID."""
    folder = image_id[:4]
    url = f"{IIIF_BASE}/{folder}%2F{image_id}/info.json"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[WARN] Could not fetch IIIF info for {image_id}: {e}", file=sys.stderr)
        return None


def download_tile(image_id: str, region: str, size: str, out_path: Path,
                  retries: int = 3) -> bool:
    """Download a single IIIF tile to out_path."""
    folder = image_id[:4]
    url = f"{IIIF_BASE}/{folder}%2F{image_id}/{region}/{size}/0/default.jpg"
    req = urllib.request.Request(url, headers=HEADERS)

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out_path.write_bytes(r.read())
            return True
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
            else:
                print(f"[WARN] Failed to download tile {url}: {e}", file=sys.stderr)
    return False


def download_map_tiles(image_id: str, out_dir: Path,
                       scale_factor: int = 4,
                       tile_size: int = 512) -> bool:
    """
    Download all tiles for a map image at a given IIIF scale factor.

    scale_factor: power-of-2 downscale (1=full res, 2=half, 4=quarter, etc.)
                  Higher values = fewer, smaller tiles. 4 is a good default.
    tile_size:    IIIF tile size in pixels (must match server's tile width).
    """
    info = get_iiif_info(image_id)
    if not info:
        return False

    full_w = info["width"]
    full_h = info["height"]

    # Effective image size at this scale factor
    scaled_w = math.ceil(full_w / scale_factor)
    scaled_h = math.ceil(full_h / scale_factor)

    # Tile size in full-resolution pixels
    region_size = tile_size * scale_factor

    cols = math.ceil(full_w / region_size)
    rows = math.ceil(full_h / region_size)

    out_dir.mkdir(parents=True, exist_ok=True)

    total = cols * rows
    print(f"  Image {image_id}: {full_w}x{full_h}px  →  {cols}x{rows} tiles "
          f"(scale 1/{scale_factor}, ~{scaled_w}x{scaled_h}px effective)")

    ok = 0
    for row in range(rows):
        for col in range(cols):
            rx = col * region_size
            ry = row * region_size
            rw = min(region_size, full_w - rx)
            rh = min(region_size, full_h - ry)

            # Actual output tile pixel dimensions
            tw = math.ceil(rw / scale_factor)
            th = math.ceil(rh / scale_factor)

            region = f"{rx},{ry},{rw},{rh}"
            size   = f"{tw},{th}"

            tile_path = out_dir / f"tile_{row:03d}_{col:03d}.jpg"
            if tile_path.exists():
                ok += 1
                continue

            if download_tile(image_id, region, size, tile_path):
                ok += 1
            print(f"  [{ok}/{total}] tile ({row},{col})", end="\r", flush=True)
            time.sleep(0.05)  # polite rate limiting

    print(f"  Downloaded {ok}/{total} tiles → {out_dir}")
    return ok == total


def assemble_tiles(image_id: str, tile_dir: Path, scale_factor: int = 4,
                   tile_size: int = 512) -> Path | None:
    """Stitch downloaded tiles into a single image using Pillow (if available)."""
    try:
        from PIL import Image
    except ImportError:
        print("[INFO] Pillow not installed — skipping assembly. "
              "Install with: pip install Pillow")
        return None

    info = get_iiif_info(image_id)
    if not info:
        return None

    full_w = info["width"]
    full_h = info["height"]
    region_size = tile_size * scale_factor
    cols = math.ceil(full_w / region_size)
    rows = math.ceil(full_h / region_size)

    scaled_w = math.ceil(full_w / scale_factor)
    scaled_h = math.ceil(full_h / scale_factor)

    canvas = Image.new("RGB", (scaled_w, scaled_h), (255, 255, 255))

    for row in range(rows):
        for col in range(cols):
            tile_path = tile_dir / f"tile_{row:03d}_{col:03d}.jpg"
            if not tile_path.exists():
                continue
            tile_img = Image.open(tile_path)
            paste_x = col * tile_size
            paste_y = row * tile_size
            canvas.paste(tile_img, (paste_x, paste_y))

    out_path = tile_dir.parent / f"{image_id}_assembled.jpg"
    canvas.save(out_path, "JPEG", quality=90)
    print(f"  Assembled → {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Download out-of-copyright historic map tiles from maps.nls.uk",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--lat",  type=float, help="Latitude (WGS84, decimal degrees)")
    p.add_argument("--lon",  type=float, help="Longitude (WGS84, decimal degrees)")
    p.add_argument("--layer", default="OS_6inch_all_find",
                   help="WFS layer typename (default: OS_6inch_all_find)")
    p.add_argument("--year-min", type=int, default=1,
                   help="Minimum publication year (default: 1)")
    p.add_argument("--year-max", type=int, default=None,
                   help=f"Maximum publication year (default: {COPYRIGHT_CUTOFF_YEAR}, i.e. >50 years ago)")
    p.add_argument("--max-maps", type=int, default=10,
                   help="Maximum number of map sheets to download (default: 10)")
    p.add_argument("--scale-factor", type=int, default=4,
                   help="IIIF scale factor: 1=full res, 2=half, 4=quarter (default: 4)")
    p.add_argument("--tile-size", type=int, default=512,
                   help="IIIF tile size in pixels (default: 512)")
    p.add_argument("--out-dir", default="nls_maps",
                   help="Output directory (default: nls_maps)")
    p.add_argument("--assemble", action="store_true",
                   help="Stitch tiles into a single image (requires Pillow)")
    p.add_argument("--list-layers", action="store_true",
                   help="List all available layer names and exit")
    p.add_argument("--list-only", action="store_true",
                   help="List matching maps without downloading")
    return p.parse_args()


def main():
    args = parse_args()

    if args.list_layers:
        print("\nAvailable layers (use with --layer):\n")
        for name, desc in AVAILABLE_LAYERS.items():
            print(f"  {name:<35} {desc}")
        print()
        return

    if args.lat is None or args.lon is None:
        print("[ERROR] --lat and --lon are required.", file=sys.stderr)
        sys.exit(1)

    year_max = args.year_max if args.year_max is not None else COPYRIGHT_CUTOFF_YEAR

    print(f"\nSearching layer '{args.layer}' at ({args.lat}, {args.lon})")
    print(f"Year range: {args.year_min} – {year_max}  (out-of-copyright cutoff: {COPYRIGHT_CUTOFF_YEAR})\n")

    maps = query_maps(
        lat=args.lat, lon=args.lon,
        typename=args.layer,
        year_min=args.year_min,
        year_max=year_max,
        max_features=args.max_maps * 3,  # fetch extra, then trim after year filter
    )

    if not maps:
        print("No out-of-copyright maps found for this location/layer.")
        return

    maps = maps[:args.max_maps]
    print(f"Found {len(maps)} map(s):\n")
    for i, m in enumerate(maps, 1):
        print(f"  {i:2}. [{m['year']}] {m['sheet']}")
        print(f"       Image ID : {m['image_id']}")
        print(f"       View URL : {m['view_url']}")
        print()

    if args.list_only:
        return

    out_root = Path(args.out_dir)
    for m in maps:
        image_id = m["image_id"]
        if not image_id:
            print(f"[SKIP] No image ID for: {m['sheet']}")
            continue

        safe_sheet = "".join(c if c.isalnum() or c in " _-" else "_" for c in m["sheet"])[:60]
        tile_dir = out_root / f"{m['year']}_{safe_sheet}_{image_id}"

        print(f"Downloading: [{m['year']}] {m['sheet']}  (ID: {image_id})")
        ok = download_map_tiles(
            image_id=image_id,
            out_dir=tile_dir,
            scale_factor=args.scale_factor,
            tile_size=args.tile_size,
        )

        if ok and args.assemble:
            assemble_tiles(
                image_id=image_id,
                tile_dir=tile_dir,
                scale_factor=args.scale_factor,
                tile_size=args.tile_size,
            )
        print()

    print(f"Done. Files saved to: {out_root.resolve()}")


if __name__ == "__main__":
    main()
