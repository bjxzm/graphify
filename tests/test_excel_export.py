from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import networkx as nx
import pytest

from graphify.exporters.excel import (
    _MAX_CELL_CHARS,
    _MAX_EXCEL_ROWS,
    _require_row_capacity,
    _safe_text,
    to_excel,
)


def _sample_graph() -> tuple[nx.DiGraph, dict[int, list[str]]]:
    graph = nx.DiGraph()
    graph.add_node(
        "program",
        label="DARPA ELGAR",
        file_type="document",
        source_file="https://www.darpa.mil/program/elgar",
        source_location="Overview",
        date="2021-08-01",
        description="Electronics for G-band Arrays program.",
    )
    graph.add_node(
        "award",
        label="=HR001122C0122",
        file_type="document",
        source_file="https://sam.gov/example",
        content="Award record\x00 with provenance.",
    )
    graph.add_edge(
        "program",
        "award",
        relation="awarded",
        confidence="EXTRACTED",
        confidence_score=0.98,
        source_file="https://sam.gov/example",
        source_location="Award notice",
        weight=1.0,
    )
    return graph, {0: ["program", "award"]}


def test_excel_cell_safety_neutralizes_formulas_and_caps_text():
    illegal = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
    assert _safe_text("=SUM(A1:A2)", illegal) == "'=SUM(A1:A2)"
    assert _safe_text("  @cmd", illegal) == "'  @cmd"
    assert "\x00" not in _safe_text("a\x00b", illegal)
    assert len(_safe_text("x" * (_MAX_CELL_CHARS + 20), illegal)) == _MAX_CELL_CHARS
    assert len(_safe_text("=" + "x" * _MAX_CELL_CHARS, illegal)) == _MAX_CELL_CHARS


def test_excel_row_limit_fails_clearly():
    _require_row_capacity("nodes", _MAX_EXCEL_ROWS - 1)
    with pytest.raises(ValueError, match="row limit"):
        _require_row_capacity("nodes", _MAX_EXCEL_ROWS)


def test_to_excel_matches_reference_layout_and_preserves_provenance(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    graph, communities = _sample_graph()
    target = tmp_path / "graph.xlsx"

    counts = to_excel(
        graph,
        communities,
        str(target),
        community_labels={0: "ELGAR Program"},
        cohesion={0: 0.75},
    )

    assert counts == {
        "annotations": 2,
        "nodes": 2,
        "relationships": 1,
        "communities": 1,
        "sources": 2,
    }
    workbook = openpyxl.load_workbook(target, data_only=False)
    assert workbook.sheetnames == ["数据标注", "节点", "关系", "社区", "来源", "元数据"]

    annotation = workbook["数据标注"]
    assert [cell.value for cell in annotation[1]] == [
        "序号",
        "时间",
        "主要标签",
        "次要标签",
        "关联标签",
        "内容",
    ]
    assert annotation.freeze_panes == "A2"
    assert annotation.max_row == 3
    labels = [annotation.cell(row=row, column=3).value for row in range(2, 4)]
    assert "'=HR001122C0122" in labels
    assert annotation["B3"].number_format == "yyyy-mm-dd"
    assert "来源：https://www.darpa.mil/program/elgar" in annotation["F3"].value

    relations = workbook["关系"]
    assert relations["C2"].value == "awarded"
    assert relations["F2"].value == "EXTRACTED"
    assert relations["G2"].value == pytest.approx(0.98)
    assert relations["I2"].value == "https://sam.gov/example"


def test_cli_export_excel_supports_custom_output(tmp_path):
    pytest.importorskip("openpyxl")
    graph, communities = _sample_graph()
    from networkx.readwrite import json_graph

    out = tmp_path / "graphify-out"
    out.mkdir()
    try:
        data = json_graph.node_link_data(graph, edges="links")
    except TypeError:
        data = json_graph.node_link_data(graph)
    (out / "graph.json").write_text(json.dumps(data), encoding="utf-8")
    (out / ".graphify_analysis.json").write_text(
        json.dumps({"communities": {"0": communities[0]}, "cohesion": {"0": 0.75}}),
        encoding="utf-8",
    )
    (out / ".graphify_labels.json").write_text(
        json.dumps({"0": "ELGAR Program"}), encoding="utf-8"
    )
    custom = tmp_path / "exports" / "elgar.xlsx"

    result = subprocess.run(
        [sys.executable, "-m", "graphify", "export", "excel", "--output", str(custom)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert custom.exists()
    assert "2 nodes, 1 relationships, 2 sources" in result.stdout
