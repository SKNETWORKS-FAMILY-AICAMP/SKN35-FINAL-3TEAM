# -*- coding: utf-8 -*-
"""양옆이 트인 표 닫기(open_edges, kordoc closeOpenTableEdges 이식) 단위 테스트."""
from agent_platform.knowledge.ingestion.parsers.pdf.open_edges import chain_collinear, close_open_edges

# 가로 괘선 4줄(전폭 100~400), 안쪽 세로선 2개(200, 300)만 있고 바깥 세로 테두리는 없는 표
HS = [(100.0, 100.0, 400.0), (130.0, 100.0, 400.0), (160.0, 100.0, 400.0), (190.0, 100.0, 400.0)]
VS = [(200.0, 100.0, 190.0), (300.0, 100.0, 190.0)]


def test_virtual_borders_added_on_both_open_sides():
    out = close_open_edges(HS, VS)
    assert sorted(round(v[0]) for v in out) == [100, 400] and all((v[1], v[2]) == (100.0, 190.0) for v in out)


def test_closed_side_not_duplicated():
    """왼쪽에 이미 세로선이 있으면 오른쪽만 합성한다."""
    out = close_open_edges(HS, VS + [(100.0, 100.0, 190.0)])
    assert [round(v[0]) for v in out] == [400]


def test_no_interior_vertical_means_not_a_table():
    """가로선만 있는 영역(밑줄·구분선)에는 테두리를 만들지 않는다."""
    assert close_open_edges(HS, []) == []


def test_chain_joins_cell_by_cell_rules():
    assert chain_collinear([(100.0, 100.0, 200.0), (100.5, 201.0, 300.0)]) == [(100.0, 100.0, 300.0)]
