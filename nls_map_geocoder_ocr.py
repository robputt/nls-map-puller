#!/usr/bin/env python3
"""
NLS Historic Map Geocoder
--------------------------
Reads downloaded NLS map tiles, extracts text labels via OCR, assigns each
label a geographic coordinate from its tile position, and stores everything
in a SQLite database that can be queried for reverse geocoding.

Workflow:
  1. Index  — scan a tile directory, OCR every tile, store labels + coords
  2. Query  — given a lat/lon, return the nearest historic place names

Usage:
    # Build the index from a seamless tile directory
    python3 nls_map_geocoder.py index --tiles nls_seamless/os_6inch/14

    # Query: what was at this location on the historic map?
    python3 nls_map_geocoder.py query --lat 50.3653 --lon -4.0845

    # Query with a custom radius (metres)
    python3 nls_map_geocoder.py query --lat 50.3653 --lon -4.0845 --radius 500

    # Query and show all results, not just the closest
    python3 nls_map_geocoder.py query --lat 50.3653 --lon -4.0845 --limit 20

Requirements:
    pip install pillow pytesseract
    brew install tesseract          # macOS
    apt install tesseract-ocr       # Debian/Ubuntu
"""

import argparse
import math
import re
import sqlite3
import sys
from pathlib import Path

try:
    from PIL import Image, ImageFilter, ImageOps
    import pytesseract
except ImportError:
    sys.exit("Missing dependencies. Run:  pip install pillow pytesseract")


# ---------------------------------------------------------------------------
# Tile coordinate math  (Web Mercator XYZ)
# ---------------------------------------------------------------------------

def tile_center_latlon(z: int, x: int, y: int) -> tuple[float, float]:
    """Return the (lat, lon) of the centre of an XYZ tile."""
    n = 2 ** z
    lon = (x + 0.5) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
    return lat, lon


def tile_pixel_latlon(z: int, x: int, y: int,
                      px: float, py: float, tile_w: int, tile_h: int) -> tuple[float, float]:
    """
    Return the (lat, lon) for a pixel position (px, py) within a tile.
    px, py are fractional pixel offsets from the tile's top-left corner.
    """
    n = 2 ** z
    # Fractional tile coordinates
    tx = x + px / tile_w
    ty = y + py / tile_h
    lon = tx / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    return lat, lon


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Image pre-processing for OCR
# ---------------------------------------------------------------------------

def preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """
    Enhance a map tile for Tesseract:
    - Convert to greyscale
    - Increase contrast
    - Sharpen
    - Upscale 2× (Tesseract works better on larger text)
    """
    img = img.convert("RGBA").convert("L")          # handles palette+transparency
    img = ImageOps.autocontrast(img, cutoff=2)      # stretch contrast
    img = img.filter(ImageFilter.SHARPEN)
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    return img


# ---------------------------------------------------------------------------
# OCR a single tile → list of (text, centre_x_frac, centre_y_frac)
# ---------------------------------------------------------------------------

# Tesseract config: treat image as sparse text (no fixed layout)
_TESS_CONFIG = "--oem 3 --psm 11"

# Minimum word length and confidence to keep
_MIN_CONF = 50
_MIN_LEN  = 3

# Regex: keep only tokens that look like real place-name words
# (letters, hyphens, apostrophes — no pure numbers or symbols)
# Must start with a capital (place names on OS maps are capitalised)
_WORD_RE = re.compile(r"^[A-Z][A-Za-z'\-]{2,}$")


def ocr_tile(img: Image.Image) -> list[tuple[str, float, float]]:
    """
    Run Tesseract on a tile image.
    Returns list of (word, x_frac, y_frac) where x/y are 0–1 fractions
    of the tile dimensions indicating where the word centre sits.
    """
    processed = preprocess_for_ocr(img)
    w, h = processed.size

    try:
        data = pytesseract.image_to_data(
            processed,
            config=_TESS_CONFIG,
            output_type=pytesseract.Output.DICT,
        )
    except pytesseract.TesseractError as e:
        print(f"  [OCR error] {e}", file=sys.stderr)
        return []

    results = []
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        try:
            conf = int(data["conf"][i])
        except (ValueError, TypeError):
            conf = 0

        if conf < _MIN_CONF:
            continue
        if len(text) < _MIN_LEN:
            continue
        if not _WORD_RE.match(text):
            continue

        # Bounding box centre as fraction of (upscaled) tile size
        bx = data["left"][i] + data["width"][i] / 2
        by = data["top"][i] + data["height"][i] / 2
        # Convert back to original tile fraction (we upscaled 2×)
        x_frac = (bx / 2) / (w / 2)
        y_frac = (by / 2) / (h / 2)

        results.append((text, x_frac, y_frac))

    return results


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS labels (
    id        INTEGER PRIMARY KEY,
    word      TEXT    NOT NULL,
    lat       REAL    NOT NULL,
    lon       REAL    NOT NULL,
    zoom      INTEGER NOT NULL,
    tile_x    INTEGER NOT NULL,
    tile_y    INTEGER NOT NULL,
    source    TEXT
);
CREATE INDEX IF NOT EXISTS idx_labels_word ON labels(word COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_labels_lat  ON labels(lat);
CREATE INDEX IF NOT EXISTS idx_labels_lon  ON labels(lon);
"""


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def insert_labels(conn: sqlite3.Connection, rows: list[tuple]):
    conn.executemany(
        "INSERT INTO labels (word, lat, lon, zoom, tile_x, tile_y, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Index command
# ---------------------------------------------------------------------------

def cmd_index(args):
    tile_dir = Path(args.tiles)
    if not tile_dir.exists():
        sys.exit(f"[ERROR] Tile directory not found: {tile_dir}")

    db_path = Path(args.db)
    conn = open_db(db_path)

    # Find all tile images — expect filenames like  {z}_{x}_{y}.png
    # (as written by nls_map_seamless_downloader.py)
    tile_files = sorted(tile_dir.rglob("*.png")) + sorted(tile_dir.rglob("*.jpg"))

    if not tile_files:
        sys.exit(f"[ERROR] No tile images found in {tile_dir}")

    print(f"Found {len(tile_files)} tiles in {tile_dir}")
    print(f"Database: {db_path}")

    total_words = 0
    for i, path in enumerate(tile_files, 1):
        # Parse z/x/y from filename  e.g. "14_4001_2764.png"
        stem = path.stem
        parts = stem.split("_")
        if len(parts) != 3:
            continue
        try:
            z, tx, ty = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue

        print(f"  [{i}/{len(tile_files)}] {path.name}", end="  ", flush=True)

        img = Image.open(path)
        tile_w, tile_h = img.size
        words = ocr_tile(img)

        rows = []
        for word, x_frac, y_frac in words:
            lat, lon = tile_pixel_latlon(z, tx, ty, x_frac * tile_w, y_frac * tile_h, tile_w, tile_h)
            rows.append((word, lat, lon, z, tx, ty, str(path)))

        if rows:
            insert_labels(conn, rows)
            total_words += len(rows)
            print(f"→ {len(rows)} labels")
        else:
            print("→ (none)")

    conn.close()
    print(f"\nDone. {total_words} labels indexed into {db_path}")


# ---------------------------------------------------------------------------
# Query command
# ---------------------------------------------------------------------------

def cmd_query(args):
    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"[ERROR] Database not found: {db_path}  — run 'index' first.")

    conn = open_db(db_path)

    lat, lon = args.lat, args.lon
    radius_m = args.radius
    limit = args.limit

    # Rough degree bounding box for the SQL pre-filter
    # 1° lat ≈ 111 km, 1° lon ≈ 111 km × cos(lat)
    dlat = radius_m / 111_000
    dlon = radius_m / (111_000 * math.cos(math.radians(lat)))

    rows = conn.execute(
        """
        SELECT word, lat, lon, zoom, tile_x, tile_y
        FROM labels
        WHERE lat BETWEEN ? AND ?
          AND lon BETWEEN ? AND ?
        """,
        (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
    ).fetchall()

    conn.close()

    if not rows:
        print(f"No labels found within {radius_m}m of ({lat}, {lon}).")
        return

    # Compute exact distances and filter
    results = []
    for word, rlat, rlon, z, tx, ty in rows:
        dist = haversine_m(lat, lon, rlat, rlon)
        if dist <= radius_m:
            results.append((dist, word, rlat, rlon))

    results.sort()
    results = results[:limit]

    if not results:
        print(f"No labels found within {radius_m}m of ({lat}, {lon}).")
        return

    print(f"\nHistoric map labels near ({lat:.5f}, {lon:.5f})  [within {radius_m}m]\n")
    print(f"  {'Distance':>10}  {'Label':<30}  {'Lat':>10}  {'Lon':>11}")
    print("  " + "-" * 68)
    for dist, word, rlat, rlon in results:
        print(f"  {dist:>9.0f}m  {word:<30}  {rlat:>10.5f}  {rlon:>11.5f}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Build and query a historic map reverse-geocoding index from NLS tiles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--db", default="nls_geocoder.db",
                   help="SQLite database path (default: nls_geocoder.db)")

    sub = p.add_subparsers(dest="cmd", required=True)

    # index
    pi = sub.add_parser("index", help="OCR tiles and build the geocoding index")
    pi.add_argument("--tiles", required=True,
                    help="Directory of downloaded tiles (e.g. nls_seamless/os_6inch/14)")

    # query
    pq = sub.add_parser("query", help="Reverse-geocode a lat/lon against the index")
    pq.add_argument("--lat",    type=float, required=True, help="Latitude")
    pq.add_argument("--lon",    type=float, required=True, help="Longitude")
    pq.add_argument("--radius", type=float, default=300,
                    help="Search radius in metres (default: 300)")
    pq.add_argument("--limit",  type=int,   default=10,
                    help="Max results to return (default: 10)")

    args = p.parse_args()

    if args.cmd == "index":
        cmd_index(args)
    elif args.cmd == "query":
        cmd_query(args)


if __name__ == "__main__":
    main()
