#!/usr/bin/env python3
"""
NLS Historic Map Geocoder — LLM edition with neighbour stitching
-----------------------------------------------------------------
Extends nls_map_geocoder_llm.py by compositing each tile with its
neighbours before passing the image to the VLM. This means place names
that straddle a tile boundary are seen whole rather than as fragments.

Strategy
--------
For every centre tile (cx, cy) in the downloaded set, a grid of
(2r+1) × (2r+1) tiles is assembled into a single composite image, where
r is the neighbour radius (default 1, giving a 3×3 grid). Missing
neighbours (edge of downloaded area) are filled with white. The VLM
analyses the composite and returns x_frac/y_frac positions relative to
the full composite. These are converted back to absolute lat/lon using
the known pixel dimensions of each constituent tile.

To avoid indexing the same label multiple times (every tile that
neighbours a given label will produce a composite containing it), a
deduplication pass merges labels that are within --dedup-radius metres
of each other and share the same normalised text.

Usage
-----
    # Build the index (3×3 neighbourhood, default)
    python3 nls_map_geocoder_llm_neighbours.py index \\
        --tiles nls_seamless/os_6inch/14

    # Larger neighbourhood (5×5) for very spread-out labels
    python3 nls_map_geocoder_llm_neighbours.py index \\
        --tiles nls_seamless/os_6inch/14 --neighbour-radius 2

    # Query (identical interface to nls_map_geocoder_llm.py)
    python3 nls_map_geocoder_llm_neighbours.py query \\
        --lat 50.3653 --lon -4.0845 --radius 500

Requirements
------------
    pip install torch torchvision transformers accelerate qwen-vl-utils pillow
"""

import argparse
import json
import math
import re
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

def _require_deps():
    missing = []
    for pkg, pip_name in [
        ("torch",         "torch torchvision"),
        ("transformers",  "transformers accelerate"),
        ("qwen_vl_utils", "qwen-vl-utils"),
        ("PIL",           "pillow"),
    ]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pip_name)
    if missing:
        sys.exit(
            "[ERROR] Missing dependencies. Install with:\n"
            f"  pip install {' '.join(missing)}"
        )


# ---------------------------------------------------------------------------
# Tile coordinate math
# ---------------------------------------------------------------------------

def tile_pixel_latlon(z: int, x: int, y: int,
                      px: float, py: float,
                      tile_w: int, tile_h: int) -> tuple[float, float]:
    """Lat/lon for pixel (px, py) within tile (z, x, y)."""
    n = 2 ** z
    tx = x + px / tile_w
    ty = y + py / tile_h
    lon = tx / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    return lat, lon


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Composite image builder
# ---------------------------------------------------------------------------

def build_composite(
    centre_x: int,
    centre_y: int,
    z: int,
    tile_index: dict,          # (x, y) -> Path
    radius: int,               # neighbourhood half-width (1 = 3×3, 2 = 5×5)
) -> tuple["Image", int, int, int, int]:
    """
    Stitch a (2r+1)×(2r+1) grid of tiles centred on (centre_x, centre_y).

    Returns:
        composite   PIL Image
        tile_w      width of a single tile in pixels
        tile_h      height of a single tile in pixels
        origin_x    tile-x of the top-left tile in the composite
        origin_y    tile-y of the top-left tile in the composite
    """
    from PIL import Image

    # Determine tile size from any available tile
    sample_path = next(iter(tile_index.values()))
    with Image.open(sample_path) as s:
        tile_w, tile_h = s.size

    grid = 2 * radius + 1
    composite = Image.new("RGB", (grid * tile_w, grid * tile_h), (255, 255, 255))

    origin_x = centre_x - radius
    origin_y = centre_y - radius

    for row in range(grid):
        for col in range(grid):
            tx = origin_x + col
            ty = origin_y + row
            path = tile_index.get((tx, ty))
            if path and path.exists():
                with Image.open(path) as tile:
                    composite.paste(tile.convert("RGB"), (col * tile_w, row * tile_h))
            # missing tiles stay white — no action needed

    return composite, tile_w, tile_h, origin_x, origin_y


def composite_frac_to_latlon(
    x_frac: float, y_frac: float,
    z: int,
    origin_x: int, origin_y: int,
    tile_w: int, tile_h: int,
    grid: int,
) -> tuple[float, float]:
    """
    Convert a fractional position within a composite image back to lat/lon.

    x_frac, y_frac are 0–1 fractions of the full composite dimensions.
    origin_x/y is the tile coordinate of the composite's top-left corner.
    """
    # Pixel position within the composite
    px_composite = x_frac * (grid * tile_w)
    py_composite = y_frac * (grid * tile_h)

    # Which tile column/row does this pixel fall in?
    col = int(px_composite // tile_w)
    row = int(py_composite // tile_h)

    # Clamp to valid range
    col = max(0, min(grid - 1, col))
    row = max(0, min(grid - 1, row))

    # Pixel offset within that tile
    px_in_tile = px_composite - col * tile_w
    py_in_tile = py_composite - row * tile_h

    tx = origin_x + col
    ty = origin_y + row

    return tile_pixel_latlon(z, tx, ty, px_in_tile, py_in_tile, tile_w, tile_h)


# ---------------------------------------------------------------------------
# VLM inference  (identical to nls_map_geocoder_llm.py)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a cartographic assistant specialising in historic British Ordnance Survey maps. "
    "When shown a map image, extract every visible text label and classify it. "
    "Return ONLY a JSON array — no prose, no markdown fences. "
    "Each element must have exactly these keys:\n"
    '  "label"    : the text exactly as it appears on the map. '
    "If a name is split across two lines or interrupted by map symbols, "
    "reconstruct the full name (e.g. 'Merafield' on one line and 'Farm' below it → 'Merafield Farm').\n"
    '  "type"     : one of: place, road, water, field, building, elevation, boundary, other\n'
    '  "x_frac"   : horizontal position of the label centre as a fraction 0.0–1.0 (left=0)\n'
    '  "y_frac"   : vertical position of the label centre as a fraction 0.0–1.0 (top=0)\n'
    "Rules:\n"
    "- Include ALL text you can see, including small labels, abbreviations (e.g. G.P., F.P., B.M.) and field names.\n"
    "- For split/two-line names, use the midpoint between the lines as y_frac.\n"
    "- Omit map symbols, scale bars, and grid numbers.\n"
    "- If no text labels are visible, return an empty array []."
)

_USER_PROMPT = (
    "Extract every text label from this historic OS map image, including any names "
    "split across two lines or interrupted by symbols. "
    "Return a JSON array as instructed."
)


def load_model(model_id: str):
    import torch
    from transformers import AutoProcessor, AutoModelForImageTextToText

    print(f"Loading model: {model_id}")
    print("(First run downloads ~8 GB — subsequent runs load from cache)")

    if torch.cuda.is_available():
        dtype, device_map = torch.float16, "auto"
        print("  Device: CUDA")
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        dtype, device_map = torch.float16, "mps"
        print("  Device: Apple MPS")
    else:
        dtype, device_map = torch.float32, "cpu"
        print("  Device: CPU (slow — consider Qwen/Qwen3-VL-2B-Instruct)")

    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForImageTextToText.from_pretrained(
        model_id, torch_dtype=dtype, device_map=device_map,
    )
    model.eval()
    return model, processor


def _preprocess(image: "Image.Image", zoom: int) -> "Image.Image":
    """
    Enhance a map tile for the VLM:
    - Convert to greyscale to remove cream/sepia tint
    - Boost contrast so ink is crisp black on white
    - Upscale only at low zoom levels where text is small
      (zoom <=13: 2×, zoom 14-15: 1.5×, zoom 16+: no upscale)
    """
    from PIL import Image, ImageOps, ImageEnhance
    img = image.convert("L")
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Sharpness(img).enhance(2.0)

    if zoom <= 13:
        scale = 2
    elif zoom <= 15:
        scale = 1.5
    else:
        scale = 1  # zoom 16+ already has large text — no upscale needed

    if scale != 1:
        img = img.resize(
            (int(img.width * scale), int(img.height * scale)),
            Image.LANCZOS,
        )
    return img.convert("RGB")


def analyse_image(image, model, processor, zoom: int = 14) -> list[dict]:
    """Run the VLM on a PIL image; return [{label, type, x_frac, y_frac}]."""
    from qwen_vl_utils import process_vision_info
    import torch

    image = _preprocess(image, zoom)

    messages = [
        {"role": "system", "content": [{"type": "text", "text": _SYSTEM_PROMPT}]},
        {"role": "user",   "content": [
            {"type": "image", "image": image},
            {"type": "text",  "text": _USER_PROMPT},
        ]},
    ]

    text_input = processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True,
    )
    image_inputs, video_inputs = process_vision_info(messages)
    inputs = processor(
        text=[text_input], images=image_inputs, videos=video_inputs,
        return_tensors="pt",
    )

    device = next(model.parameters()).device
    inputs = {k: v.to(device) if isinstance(v, torch.Tensor) else v
              for k, v in inputs.items()}

    with torch.inference_mode():
        generated_ids = model.generate(**inputs, max_new_tokens=1024, do_sample=False)

    trimmed = [out[len(inp):] for inp, out in zip(inputs["input_ids"], generated_ids)]
    raw = processor.batch_decode(
        trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False,
    )[0].strip()

    return _parse_response(raw)


def _parse_response(raw: str) -> list[dict]:
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        kind  = str(item.get("type",  "other")).strip().lower()
        try:
            x_frac = float(item.get("x_frac", 0.5))
            y_frac = float(item.get("y_frac", 0.5))
        except (TypeError, ValueError):
            x_frac, y_frac = 0.5, 0.5
        if not label:
            continue
        results.append({
            "label":  label,
            "type":   kind,
            "x_frac": max(0.0, min(1.0, x_frac)),
            "y_frac": max(0.0, min(1.0, y_frac)),
        })
    return results


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(rows: list[tuple], radius_m: float, neighbour_radius: int) -> list[tuple]:
    """
    Cluster rows that represent the same label instance.

    Two rows are merged when:
      1. Their label text matches (case-insensitive, or one is a substring), AND
      2. Their coordinates are within radius_m of each other.

    Within each cluster the longest (most complete) label wins.

    The neighbour_radius parameter is accepted for API compatibility but is no
    longer used as a filter — geographic proximity is sufficient to scope
    deduplication correctly, since labels from non-overlapping areas will
    naturally be further apart than any reasonable radius_m.

    rows: list of (label, type, lat, lon, zoom, tile_x, tile_y, source)
    """
    clusters: list[tuple] = []

    for row in rows:
        label, lat, lon = row[0], row[2], row[3]
        norm = label.lower().strip()

        matched = False
        for idx, rep in enumerate(clusters):
            rep_norm = rep[0].lower().strip()

            text_matches = (
                norm == rep_norm or
                norm in rep_norm or
                rep_norm in norm
            )
            if not text_matches:
                continue

            if haversine_m(lat, lon, rep[2], rep[3]) <= radius_m:
                if len(label) > len(rep[0]):
                    clusters[idx] = row
                matched = True
                break

        if not matched:
            clusters.append(row)

    return clusters


# ---------------------------------------------------------------------------
# Database  (identical schema to nls_map_geocoder_llm.py)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS labels (
    id        INTEGER PRIMARY KEY,
    label     TEXT    NOT NULL,
    type      TEXT    NOT NULL DEFAULT 'other',
    lat       REAL    NOT NULL,
    lon       REAL    NOT NULL,
    zoom      INTEGER NOT NULL,
    tile_x    INTEGER NOT NULL,
    tile_y    INTEGER NOT NULL,
    source    TEXT
);
CREATE INDEX IF NOT EXISTS idx_label_text ON labels(label COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_label_lat  ON labels(lat);
CREATE INDEX IF NOT EXISTS idx_label_lon  ON labels(lon);
CREATE INDEX IF NOT EXISTS idx_label_type ON labels(type);
"""


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def insert_labels(conn: sqlite3.Connection, rows: list[tuple]):
    conn.executemany(
        "INSERT INTO labels (label, type, lat, lon, zoom, tile_x, tile_y, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Index command
# ---------------------------------------------------------------------------

def cmd_index(args):
    _require_deps()

    tile_dir = Path(args.tiles)
    if not tile_dir.exists():
        sys.exit(f"[ERROR] Tile directory not found: {tile_dir}")

    # Build a (tx, ty) → Path index of all available tiles
    all_files = sorted(tile_dir.rglob("*.png")) + sorted(tile_dir.rglob("*.jpg"))
    if not all_files:
        sys.exit(f"[ERROR] No tile images found in {tile_dir}")

    tile_index: dict[tuple[int, int], Path] = {}
    zoom = None
    for path in all_files:
        parts = path.stem.split("_")
        if len(parts) != 3:
            continue
        try:
            z, tx, ty = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if zoom is None:
            zoom = z
        elif z != zoom:
            print(f"[WARN] Mixed zoom levels detected — skipping {path.name}")
            continue
        tile_index[(tx, ty)] = path

    if not tile_index:
        sys.exit("[ERROR] No valid {z}_{x}_{y} tile files found.")

    radius  = args.neighbour_radius
    grid    = 2 * radius + 1
    dedup_r = args.dedup_radius
    db_path = Path(args.db)
    conn    = open_db(db_path)
    model, processor = load_model(args.model)

    # centres sorted by (ty, tx) so we process row by row
    centres = sorted(tile_index.keys(), key=lambda c: (c[1], c[0]))
    print(f"\nIndexing {len(centres)} tiles as {grid}×{grid} composites → {db_path}\n")

    # composite_rows[(cx, cy)] = rows extracted from that composite
    composite_rows: dict[tuple[int, int], list[tuple]] = {}
    flushed: set[tuple[int, int]] = set()   # tiles already written to DB
    total_written = 0

    for i, (cx, cy) in enumerate(centres, 1):
        print(f"  [{i}/{len(centres)}] centre tile z{zoom}/{cx}/{cy}", end="  ", flush=True)

        composite, tile_w, tile_h, origin_x, origin_y = build_composite(
            cx, cy, zoom, tile_index, radius,
        )
        features = analyse_image(composite, model, processor, zoom)

        rows = []
        for feat in features:
            lat, lon = composite_frac_to_latlon(
                feat["x_frac"], feat["y_frac"],
                zoom, origin_x, origin_y, tile_w, tile_h, grid,
            )
            rows.append((
                feat["label"], feat["type"],
                lat, lon,
                zoom, cx, cy,
                f"composite({cx},{cy},r={radius})",
            ))

        composite_rows[(cx, cy)] = rows

        summary = ", ".join(f"{r[0]} ({r[1]})" for r in rows[:3])
        if len(rows) > 3:
            summary += f" … +{len(rows)-3} more"
        print(f"→ {len(rows)} labels" + (f": {summary}" if rows else ""))

        # Flush any tile (tx, ty) whose last possible covering composite
        # (tx+radius, ty+radius) has now been processed — i.e. cx >= tx+radius
        # AND cy >= ty+radius — and which hasn't been flushed yet.
        to_flush = [
            t for t in tile_index
            if t not in flushed
            and t[0] + radius <= cx
            and t[1] + radius <= cy
        ]
        for t in to_flush:
            tx, ty = t
            covering = [
                row
                for (ocx, ocy), orows in composite_rows.items()
                for row in orows
                if abs(ocx - tx) <= radius and abs(ocy - ty) <= radius
            ]
            deduped = deduplicate(covering, dedup_r, radius)
            insert_labels(conn, deduped)
            total_written += len(deduped)
            flushed.add(t)
            print(
                f"  → DB write: tile z{zoom}/{tx}/{ty} fully covered "
                f"({len(covering)} raw → {len(deduped)} labels stored, "
                f"{total_written} total)",
                flush=True,
            )

        # Evict composite_rows that can no longer contribute to any un-flushed tile.
        # A composite (ocx, ocy) is stale when every tile it could cover is flushed,
        # i.e. all tiles (tx,ty) with |tx-ocx|<=r and |ty-ocy|<=r are in flushed.
        stale = [
            k for k in list(composite_rows.keys())
            if all(
                (k[0] + dx, k[1] + dy) in flushed
                for dx in range(-radius, radius + 1)
                for dy in range(-radius, radius + 1)
                if (k[0] + dx, k[1] + dy) in tile_index
            )
        ]
        for k in stale:
            del composite_rows[k]

    # Flush remaining tiles (right/bottom edges never satisfied the mid-loop condition)
    unflushed = [t for t in tile_index if t not in flushed]
    if unflushed:
        print(f"\nFlushing {len(unflushed)} remaining edge tile(s) …")
        for t in unflushed:
            tx, ty = t
            covering = [
                row
                for (ocx, ocy), orows in composite_rows.items()
                for row in orows
                if abs(ocx - tx) <= radius and abs(ocy - ty) <= radius
            ]
            deduped = deduplicate(covering, dedup_r, radius)
            insert_labels(conn, deduped)
            total_written += len(deduped)
            flushed.add(t)
            print(
                f"  → DB write: tile z{zoom}/{tx}/{ty} (edge) "
                f"({len(covering)} raw → {len(deduped)} labels stored, "
                f"{total_written} total)",
                flush=True,
            )

    conn.close()
    print(f"\nDone. {total_written} labels indexed into {db_path}")


# ---------------------------------------------------------------------------
# Query command  (identical to nls_map_geocoder_llm.py)
# ---------------------------------------------------------------------------

def cmd_query(args):
    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"[ERROR] Database not found: {db_path}  — run 'index' first.")

    conn = open_db(db_path)
    lat, lon   = args.lat, args.lon
    radius_m   = args.radius
    limit      = args.limit
    type_filter = args.type

    dlat = radius_m / 111_000
    dlon = radius_m / (111_000 * math.cos(math.radians(lat)))

    sql = """
        SELECT label, type, lat, lon
        FROM labels
        WHERE lat BETWEEN ? AND ?
          AND lon BETWEEN ? AND ?
    """
    params: list = [lat - dlat, lat + dlat, lon - dlon, lon + dlon]

    if type_filter:
        sql += " AND type = ?"
        params.append(type_filter.lower())

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    results = []
    for label, kind, rlat, rlon in rows:
        dist = haversine_m(lat, lon, rlat, rlon)
        if dist <= radius_m:
            results.append((dist, label, kind, rlat, rlon))

    results.sort()
    results = results[:limit]

    if not results:
        msg = f"No labels found within {radius_m}m of ({lat}, {lon})"
        if type_filter:
            msg += f" with type '{type_filter}'"
        print(msg + ".")
        return

    type_note = f"  type={type_filter}" if type_filter else ""
    print(f"\nHistoric map labels near ({lat:.5f}, {lon:.5f})  "
          f"[within {radius_m:.0f}m{type_note}]\n")
    print(f"  {'Distance':>10}  {'Type':<12}  {'Label':<35}  {'Lat':>10}  {'Lon':>11}")
    print("  " + "-" * 82)
    for dist, label, kind, rlat, rlon in results:
        print(f"  {dist:>9.0f}m  {kind:<12}  {label:<35}  {rlat:>10.5f}  {rlon:>11.5f}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "Qwen/Qwen3-VL-4B-Instruct"


def main():
    p = argparse.ArgumentParser(
        description="LLM geocoder with neighbour-tile stitching for complete place names",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--db", default="nls_geocoder_llm_neighbours.db",
                   help="SQLite database path (default: nls_geocoder_llm_neighbours.db)")

    sub = p.add_subparsers(dest="cmd", required=True)

    # index
    pi = sub.add_parser("index", help="Build the geocoding index using neighbour composites")
    pi.add_argument("--tiles", required=True,
                    help="Directory of downloaded tiles (e.g. nls_seamless/os_6inch/14)")
    pi.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"HuggingFace model ID (default: {DEFAULT_MODEL})")
    pi.add_argument("--neighbour-radius", type=int, default=1, metavar="R",
                    help="Neighbourhood half-width: 1=3×3, 2=5×5 (default: 1)")
    pi.add_argument("--dedup-radius", type=float, default=50, metavar="METRES",
                    help="Merge duplicate labels within this distance in metres (default: 50)")

    # query
    pq = sub.add_parser("query", help="Reverse-geocode a lat/lon against the index")
    pq.add_argument("--lat",    type=float, required=True)
    pq.add_argument("--lon",    type=float, required=True)
    pq.add_argument("--radius", type=float, default=300,
                    help="Search radius in metres (default: 300)")
    pq.add_argument("--limit",  type=int,   default=10,
                    help="Max results (default: 10)")
    pq.add_argument("--type",   default=None,
                    help="Filter by type: place, road, water, field, "
                         "building, elevation, boundary, other")

    args = p.parse_args()
    if args.cmd == "index":
        cmd_index(args)
    elif args.cmd == "query":
        cmd_query(args)


if __name__ == "__main__":
    main()
