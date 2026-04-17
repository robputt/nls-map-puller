#!/usr/bin/env python3
"""
NLS Historic Map Geocoder — LLM edition
-----------------------------------------
Uses Qwen3-VL-4B-Instruct (a local vision-language model) to extract place
names and map features from downloaded NLS tile images, then stores them in
a SQLite database for reverse-geocoding queries.

Unlike the OCR approach, the VLM understands map context: it can distinguish
place names from contour labels, road names from field boundaries, and returns
structured JSON rather than raw character soup.

Usage:
    # Build the index from a seamless tile directory
    python3 nls_map_geocoder_llm.py index --tiles nls_seamless/os_6inch/14

    # Use a different model (e.g. larger 7B variant)
    python3 nls_map_geocoder_llm.py index --tiles nls_seamless/os_6inch/14 \\
        --model Qwen/Qwen3-VL-7B-Instruct

    # Query: what was at this location on the historic map?
    python3 nls_map_geocoder_llm.py query --lat 50.3653 --lon -4.0845

    # Wider search with more results
    python3 nls_map_geocoder_llm.py query --lat 50.3653 --lon -4.0845 \\
        --radius 1000 --limit 20

Requirements:
    pip install torch torchvision transformers accelerate qwen-vl-utils pillow

The model (~8 GB) is downloaded from HuggingFace on first run and cached
locally. Subsequent runs load from cache.

Hardware notes:
    - Apple Silicon (M2 Pro+ / 16 GB+): runs via MPS, ~4-10 tok/s
    - NVIDIA GPU (8 GB+ VRAM): runs via CUDA in float16, ~10-25 tok/s
    - CPU only: works but slow (~0.5-2 tok/s); use --model Qwen/Qwen3-VL-2B-Instruct
"""

import argparse
import json
import math
import re
import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Lazy imports — checked at runtime so the file can be imported without GPU deps
# ---------------------------------------------------------------------------

def _require_deps():
    missing = []
    try:
        import torch  # noqa: F401
    except ImportError:
        missing.append("torch torchvision")
    try:
        import transformers  # noqa: F401
    except ImportError:
        missing.append("transformers accelerate")
    try:
        import qwen_vl_utils  # noqa: F401
    except ImportError:
        missing.append("qwen-vl-utils")
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing.append("pillow")
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
    n = 2 ** z
    tx = x + px / tile_w
    ty = y + py / tile_h
    lon = tx / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    return lat, lon


def tile_center_latlon(z: int, x: int, y: int) -> tuple[float, float]:
    return tile_pixel_latlon(z, x, y, 0.5, 0.5, 1, 1)


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# VLM inference
# ---------------------------------------------------------------------------

# Prompt asking the model to return structured JSON
_SYSTEM_PROMPT = (
    "You are a cartographic assistant specialising in historic British Ordnance Survey maps. "
    "When shown a map tile, extract every visible text label and classify it. "
    "Return ONLY a JSON array — no prose, no markdown fences. "
    "Each element must have exactly these keys:\n"
    '  "label"    : the text exactly as it appears on the map\n'
    '  "type"     : one of: place, road, water, field, building, elevation, boundary, other\n'
    '  "x_frac"   : horizontal position of the label centre as a fraction 0.0–1.0 (left=0)\n'
    '  "y_frac"   : vertical position of the label centre as a fraction 0.0–1.0 (top=0)\n'
    "Omit map symbols, scale bars, and grid numbers. "
    "If no text labels are visible, return an empty array []."
)

_USER_PROMPT = (
    "Extract all text labels from this historic OS map tile. "
    "Return a JSON array as instructed."
)


def load_model(model_id: str):
    """Load the Qwen3-VL model and processor, auto-selecting device."""
    import torch
    from transformers import AutoProcessor, AutoModelForImageTextToText

    print(f"Loading model: {model_id}")
    print("(First run downloads ~8 GB — subsequent runs load from cache)")

    # Pick dtype and device
    if torch.cuda.is_available():
        dtype = torch.float16
        device_map = "auto"
        print("  Device: CUDA")
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        dtype = torch.float16
        device_map = "mps"
        print("  Device: Apple MPS")
    else:
        dtype = torch.float32
        device_map = "cpu"
        print("  Device: CPU (slow — consider Qwen/Qwen3-VL-2B-Instruct)")

    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForImageTextToText.from_pretrained(
        model_id,
        torch_dtype=dtype,
        device_map=device_map,
    )
    model.eval()
    return model, processor


def analyse_tile(image, model, processor) -> list[dict]:
    """
    Run the VLM on a single PIL image.
    Returns a list of dicts: {label, type, x_frac, y_frac}
    """
    from qwen_vl_utils import process_vision_info

    messages = [
        {
            "role": "system",
            "content": [{"type": "text", "text": _SYSTEM_PROMPT}],
        },
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text",  "text": _USER_PROMPT},
            ],
        },
    ]

    text_input = processor.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    image_inputs, video_inputs = process_vision_info(messages)
    inputs = processor(
        text=[text_input],
        images=image_inputs,
        videos=video_inputs,
        return_tensors="pt",
    )

    # Move inputs to the same device as the model
    import torch
    device = next(model.parameters()).device
    inputs = {k: v.to(device) if isinstance(v, torch.Tensor) else v
              for k, v in inputs.items()}

    with torch.inference_mode():
        generated_ids = model.generate(
            **inputs,
            max_new_tokens=1024,
            do_sample=False,
        )

    # Strip the prompt tokens from the output
    trimmed = [
        out[len(inp):]
        for inp, out in zip(inputs["input_ids"], generated_ids)
    ]
    raw = processor.batch_decode(
        trimmed,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )[0].strip()

    return _parse_response(raw)


def _parse_response(raw: str) -> list[dict]:
    """Extract and validate the JSON array from the model's response."""
    # Strip any accidental markdown fences
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

    # Find the outermost [...] array
    start = raw.find("[")
    end   = raw.rfind("]")
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
        label  = str(item.get("label", "")).strip()
        kind   = str(item.get("type",  "other")).strip().lower()
        try:
            x_frac = float(item.get("x_frac", 0.5))
            y_frac = float(item.get("y_frac", 0.5))
        except (TypeError, ValueError):
            x_frac, y_frac = 0.5, 0.5

        if not label:
            continue

        # Clamp fractions to valid range
        x_frac = max(0.0, min(1.0, x_frac))
        y_frac = max(0.0, min(1.0, y_frac))

        results.append({
            "label":  label,
            "type":   kind,
            "x_frac": x_frac,
            "y_frac": y_frac,
        })

    return results


# ---------------------------------------------------------------------------
# Database
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
    from PIL import Image

    tile_dir = Path(args.tiles)
    if not tile_dir.exists():
        sys.exit(f"[ERROR] Tile directory not found: {tile_dir}")

    db_path = Path(args.db)
    conn = open_db(db_path)

    tile_files = sorted(tile_dir.rglob("*.png")) + sorted(tile_dir.rglob("*.jpg"))
    if not tile_files:
        sys.exit(f"[ERROR] No tile images found in {tile_dir}")

    model, processor = load_model(args.model)

    print(f"\nIndexing {len(tile_files)} tiles → {db_path}\n")
    total_labels = 0

    for i, path in enumerate(tile_files, 1):
        stem = path.stem
        parts = stem.split("_")
        if len(parts) != 3:
            continue
        try:
            z, tx, ty = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue

        print(f"  [{i}/{len(tile_files)}] {path.name}", end="  ", flush=True)

        img = Image.open(path).convert("RGB")
        tile_w, tile_h = img.size

        features = analyse_tile(img, model, processor)

        rows = []
        for feat in features:
            lat, lon = tile_pixel_latlon(
                z, tx, ty,
                feat["x_frac"] * tile_w,
                feat["y_frac"] * tile_h,
                tile_w, tile_h,
            )
            rows.append((feat["label"], feat["type"], lat, lon, z, tx, ty, str(path)))

        if rows:
            insert_labels(conn, rows)
            total_labels += len(rows)
            label_summary = ", ".join(
                f"{r[0]} ({r[1]})" for r in rows[:4]
            )
            if len(rows) > 4:
                label_summary += f" … +{len(rows)-4} more"
            print(f"→ {len(rows)} labels: {label_summary}")
        else:
            print("→ (none)")

    conn.close()
    print(f"\nDone. {total_labels} labels indexed into {db_path}")


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
    type_filter = args.type

    dlat = radius_m / 111_000
    dlon = radius_m / (111_000 * math.cos(math.radians(lat)))

    sql = """
        SELECT label, type, lat, lon
        FROM labels
        WHERE lat BETWEEN ? AND ?
          AND lon BETWEEN ? AND ?
    """
    params = [lat - dlat, lat + dlat, lon - dlon, lon + dlon]

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
        description="LLM-powered historic map reverse geocoder using Qwen3-VL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--db", default="nls_geocoder_llm.db",
                   help="SQLite database path (default: nls_geocoder_llm.db)")

    sub = p.add_subparsers(dest="cmd", required=True)

    # index
    pi = sub.add_parser("index", help="Analyse tiles with VLM and build the geocoding index")
    pi.add_argument("--tiles",  required=True,
                    help="Directory of downloaded tiles (e.g. nls_seamless/os_6inch/14)")
    pi.add_argument("--model",  default=DEFAULT_MODEL,
                    help=f"HuggingFace model ID (default: {DEFAULT_MODEL})")

    # query
    pq = sub.add_parser("query", help="Reverse-geocode a lat/lon against the index")
    pq.add_argument("--lat",    type=float, required=True)
    pq.add_argument("--lon",    type=float, required=True)
    pq.add_argument("--radius", type=float, default=300,
                    help="Search radius in metres (default: 300)")
    pq.add_argument("--limit",  type=int,   default=10,
                    help="Max results (default: 10)")
    pq.add_argument("--type",   default=None,
                    help="Filter by feature type: place, road, water, field, "
                         "building, elevation, boundary, other")

    args = p.parse_args()
    if args.cmd == "index":
        cmd_index(args)
    elif args.cmd == "query":
        cmd_query(args)


if __name__ == "__main__":
    main()
