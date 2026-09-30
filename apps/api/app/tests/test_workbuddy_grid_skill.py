"""The bundled WorkBuddy grid formatter must satisfy the server's SHEET contract."""

import importlib.util
import json
from pathlib import Path

import pytest

from app.modules.external_extraction.document_schemas import DocumentExtractionUnit


SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "integrations/workbuddy/file-agent-plugin/skills/file-agent-excel-grid-archive/scripts/grid_to_payload.py"
)


def load_formatter():
    spec = importlib.util.spec_from_file_location("workbuddy_grid_to_payload", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_multiline_grid_header_preserves_exact_cells_and_server_contract(tmp_path):
    formatter = load_formatter()
    grid = {"cells": [
        {"row": 1, "col": 0, "string_value": "姓名"},
        {"row": 1, "col": 4, "string_value": "教学\n科研"},
        {"row": 2, "col": 8, "string_value": "任职信息"},
        {"row": 4, "col": 0, "number_value": 1.0},
    ]}
    source = tmp_path / "grid.json"
    source.write_text(json.dumps(grid, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "result"

    assert formatter.main([
        "--grid", str(source), "--sheet", "教学科研教授", "--header", "A2:I3",
        "--rows", "5", "--cols", "9", "--require-a-i", "--out", str(output),
    ]) == 0
    unit = json.loads((tmp_path / "result_unit.json").read_text(encoding="utf-8"))
    accepted = DocumentExtractionUnit.model_validate(unit)
    assert accepted.sheet_name == "教学科研教授"
    assert len(accepted.blocks) == 3
    multiline = next(block for block in accepted.blocks if block.column_index == 4)
    assert multiline.text == "教学\n科研"
    assert accepted.text[multiline.start:multiline.end] == multiline.text
    assert "sheet_name" not in unit["blocks"][0]
    assert "\n" in accepted.text
    assert "1" in accepted.text
    # Physical rows come from the grid coordinates, not splitlines() of cell text.
    assert len(accepted.text.splitlines()) > 5


def test_grid_formatter_rejects_fabricated_or_truncated_coordinates():
    formatter = load_formatter()
    grid = {"cells": [
        {"row": 1, "col": 4, "string_value": "标题"},
        {"row": 2, "col": 8, "string_value": "尾列"},
    ]}
    assert DocumentExtractionUnit.model_validate(formatter.build_unit(
        grid, sheet="普通表", header="B2:I3", order=0, unit_id="sheet-0",
    ))
    with pytest.raises(ValueError, match="start at A"):
        formatter.build_unit(grid, sheet="范围表", header="B2:I3", order=0,
                             unit_id="sheet-0", require_a_i=True)
    with pytest.raises(ValueError, match="discard cells"):
        formatter.build_unit(grid, sheet="截断表", header="A2:I3", order=0,
                             unit_id="sheet-0", cols=8)
    with pytest.raises(ValueError, match="duplicate coordinates"):
        formatter.build_unit({"cells": grid["cells"] + [grid["cells"][0]]},
                             sheet="重复表", header="A2:I3", order=0, unit_id="sheet-0")
