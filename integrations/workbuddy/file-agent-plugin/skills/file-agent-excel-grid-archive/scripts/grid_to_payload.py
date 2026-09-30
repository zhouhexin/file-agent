#!/usr/bin/env python3
"""Turn a verified WorkBuddy cell grid into one File Agent SHEET unit.

The grid is evidence from a spreadsheet reader, not a way to parse XLS files.
Cell text is kept verbatim so header comparison against the converted workbook
also works when a header contains a newline.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_TEXT_CHARS = 100_000
MAX_BLOCKS = 2_000
MAX_BLOCK_CHARS = 20_000
MAX_GRID_POSITIONS = 100_000


def column_index(label: str) -> int:
    value = 0
    for char in label.upper():
        value = value * 26 + ord(char) - ord("A") + 1
    return value - 1


def header_bounds(value: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"([A-Za-z]+)([1-9]\d*):([A-Za-z]+)([1-9]\d*)", value.strip())
    if match is None:
        raise ValueError("--header must be an Excel range such as A2:AA4")
    left, top, right, bottom = match.groups()
    bounds = (int(top) - 1, int(bottom) - 1, column_index(left), column_index(right))
    if bounds[0] > bounds[1] or bounds[2] > bounds[3]:
        raise ValueError("--header has reversed bounds")
    return bounds


def cell_text(cell: dict) -> str | None:
    text = cell.get("string_value")
    if text is not None and str(text) != "":
        return str(text)
    number = cell.get("number_value")
    if number is None:
        return None
    if isinstance(number, bool):
        return "TRUE" if number else "FALSE"
    try:
        decimal = Decimal(str(number))
    except InvalidOperation as exc:
        raise ValueError("grid contains an invalid number_value") from exc
    if not decimal.is_finite():
        raise ValueError("grid contains a non-finite number_value")
    return format(decimal, "f").rstrip("0").rstrip(".") if decimal != decimal.to_integral_value() else format(decimal, "f").split(".")[0]


def build_unit(
    raw: dict, *, sheet: str, header: str, order: int, unit_id: str,
    require_a_i: bool = False, rows: int | None = None, cols: int | None = None,
) -> dict:
    cells = raw.get("cells") if isinstance(raw, dict) else None
    if not isinstance(cells, list) or not cells:
        raise ValueError("grid must contain a nonempty cells list")
    if not sheet or len(sheet) > 255 or not unit_id or len(unit_id) > 120 or order < 0:
        raise ValueError("invalid sheet, unit_id, or order")
    top, bottom, left, right = header_bounds(header)
    if require_a_i and (left != 0 or right < 8):
        raise ValueError("A–I scoped target header must start at A and cover I")

    values: dict[tuple[int, int], str | None] = {}
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("grid cell must be an object")
        row, col = cell.get("row"), cell.get("col")
        if type(row) is not int or type(col) is not int or row < 0 or col < 0:
            raise ValueError("grid coordinates must be nonnegative integers")
        if (row, col) in values:
            raise ValueError("grid contains duplicate coordinates")
        values[row, col] = cell_text(cell)

    height = rows if rows is not None else max(row for row, _ in values) + 1
    width = cols if cols is not None else max(col for _, col in values) + 1
    if type(height) is not int or type(width) is not int or height <= 0 or width <= 0:
        raise ValueError("grid dimensions must be positive integers")
    if height * width > MAX_GRID_POSITIONS:
        raise ValueError("grid dimensions are too large")
    if any(row >= height or col >= width for row, col in values):
        raise ValueError("grid dimensions would discard cells")
    if bottom >= height or right >= width:
        raise ValueError("header range exceeds the real grid")

    chunks: list[str] = []
    offsets: dict[tuple[int, int], int] = {}
    position = 0
    for row in range(height):
        if row:
            chunks.append("\n")
            position += 1
        for col in range(width):
            if col:
                chunks.append("\t")
                position += 1
            offsets[row, col] = position
            value = values.get((row, col)) or ""
            chunks.append(value)
            position += len(value)
    text = "".join(chunks)
    if not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise ValueError("SHEET text is empty or exceeds the service limit")

    blocks = []
    expected = {
        (row, col) for (row, col), value in values.items()
        if top <= row <= bottom and left <= col <= right and value and value.strip()
    }
    if not expected:
        raise ValueError("header range has no nonempty grid cells")
    if len(expected) > MAX_BLOCKS:
        raise ValueError("header has more TABLE_CELL blocks than the service permits")
    for row, col in sorted(expected):
        value = values[row, col]
        if len(value) > MAX_BLOCK_CHARS:
            raise ValueError("header cell exceeds the service block limit")
        start = offsets[row, col]
        block = {
            "block_id": f"r{row}c{col}", "kind": "TABLE_CELL", "text": value,
            "start": start, "end": start + len(value),
            "table_id": "T1", "row_index": row, "column_index": col,
        }
        if text[block["start"]:block["end"]] != value:
            raise ValueError("TABLE_CELL text does not match unit text")
        blocks.append(block)
    if {(block["row_index"], block["column_index"]) for block in blocks} != expected:
        raise ValueError("header block coordinates are incomplete")
    return {
        "unit_id": unit_id, "kind": "SHEET", "sheet_name": sheet,
        "order": order, "status": "READ", "text": text, "blocks": blocks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid", required=True, type=Path)
    parser.add_argument("--sheet", required=True)
    parser.add_argument("--header", required=True)
    parser.add_argument("--out", required=True, type=Path, help="output file prefix")
    parser.add_argument("--order", type=int, default=0)
    parser.add_argument("--unit-id")
    parser.add_argument("--rows", type=int)
    parser.add_argument("--cols", type=int)
    parser.add_argument("--require-a-i", action="store_true")
    args = parser.parse_args(argv)
    try:
        raw = json.loads(args.grid.read_text(encoding="utf-8"))
        unit = build_unit(
            raw, sheet=args.sheet, header=args.header, order=args.order,
            unit_id=args.unit_id or f"sheet-{args.order}",
            require_a_i=args.require_a_i, rows=args.rows, cols=args.cols,
        )
        output = args.out
        output.parent.mkdir(parents=True, exist_ok=True)
        output.with_name(output.name + "_unit_text.txt").write_text(unit["text"], encoding="utf-8")
        output.with_name(output.name + "_blocks.json").write_text(
            json.dumps(unit["blocks"], ensure_ascii=False, indent=2), encoding="utf-8",
        )
        output.with_name(output.name + "_unit.json").write_text(
            json.dumps(unit, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    print(f"[OK] SHEET unit ready: {len(unit['blocks'])} header cells, {len(unit['text'])} characters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
