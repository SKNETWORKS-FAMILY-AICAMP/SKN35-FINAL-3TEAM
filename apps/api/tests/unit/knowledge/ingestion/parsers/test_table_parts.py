# -*- coding: utf-8 -*-
"""쪽 넘김 클립 표 조각 잇기(table_parts, kordoc table-parts 이식) 단위 테스트."""
from agent_platform.knowledge.ingestion.parsers.pdf.pdf_graphics import Rect
from agent_platform.knowledge.ingestion.parsers.pdf.table_parts import Part, PCell, RowRules, merge_parts

H = 800.0


def _part(page, top, bottom, colXs, cells, rules=None, cont=None):
    rows = max(a.r + a.rs for a in cells)
    return Part(page, H, Rect(colXs[0], bottom, colXs[-1], top), colXs, rows, len(colXs) - 1, cells, rules, cont)


def _always(*_):
    return True


def _grid(p):
    return {(a.r, a.c): (a.rs, a.cs, a.text) for a in p.cells if not a.filler}


def test_repeated_header_dropped_and_rows_appended():
    """같은 열 짜임 두 조각(쪽 끝까지 / 쪽 첫머리부터): 되풀이 머리 행은 빼고 행을 붙인다."""
    xs = [100.0, 200.0, 300.0]
    a = _part(1, 700, 60, xs, [PCell(0, 0, 1, 1, "구분"), PCell(0, 1, 1, 1, "내용"),
                               PCell(1, 0, 1, 1, "가"), PCell(1, 1, 1, 1, "하나")])
    b = _part(2, 760, 500, xs, [PCell(0, 0, 1, 1, "구분"), PCell(0, 1, 1, 1, "내용"),
                                PCell(1, 0, 1, 1, "나"), PCell(1, 1, 1, 1, "둘")])
    (t,) = merge_parts([a, b], _always)
    assert (t.rows, t.cols) == (3, 2) and _grid(t)[(2, 0)][2] == "나"


def test_carried_vertical_merge_extends_rowspan():
    """앞 쪽 마지막 행의 왼쪽 칸이 다음 쪽으로 이어지면 다음 쪽엔 그 열 클립이 없다 → 합집합 격자에서 세로 병합을 늘린다."""
    a = _part(1, 700, 60, [100.0, 200.0, 300.0],
              [PCell(0, 0, 1, 1, "분류"), PCell(0, 1, 1, 1, "항목"),
               PCell(1, 0, 1, 1, "갑"), PCell(1, 1, 1, 1, "첫째")],
              RowRules(True, True, 3, 0))
    b = _part(2, 760, 600, [200.0, 300.0], [PCell(0, 0, 1, 1, "둘째"), PCell(1, 0, 1, 1, "셋째")],
              RowRules(True, True, 1, 0))
    (t,) = merge_parts([a, b], _always)
    g = _grid(t)
    assert (t.rows, t.cols) == (4, 2)
    assert g[(1, 0)] == (3, 1, "갑") and g[(3, 1)][2] == "셋째"


def test_not_joined_when_text_between_or_not_at_page_edge():
    xs = [100.0, 200.0, 300.0]
    mk = lambda p, top, bot: _part(p, top, bot, xs, [PCell(0, 0, 1, 1, "가"), PCell(0, 1, 1, 1, "나")])
    assert len(merge_parts([mk(1, 700, 60), mk(2, 760, 600)], lambda *_: False)) == 2   # 사이 글
    assert len(merge_parts([mk(1, 700, 400), mk(2, 760, 600)], _always)) == 2           # 앞 표가 쪽 끝까지 안 참


def test_annex_head_starts_new_table():
    xs = [100.0, 200.0, 300.0]
    a = _part(1, 700, 60, xs, [PCell(0, 0, 1, 1, "가"), PCell(0, 1, 1, 1, "나")])
    b = _part(2, 760, 600, xs, [PCell(0, 0, 1, 1, "붙임 2"), PCell(0, 1, 1, 1, "제목")])
    assert len(merge_parts([a, b], _always)) == 2


def test_one_cell_continuation_appended_to_cell():
    """앞 쪽 마지막 칸의 이어짐 1칸 조각은 새 행이 아니라 그 칸 글에 붙는다."""
    xs = [100.0, 200.0, 300.0]
    a = _part(1, 700, 60, xs, [PCell(0, 0, 1, 1, "근거"), PCell(0, 1, 1, 1, "앞 글")])
    b = _part(2, 760, 600, [200.0, 300.0], [PCell(0, 0, 1, 1, "뒤 글")], cont=(200.0, 300.0))
    (t,) = merge_parts([a, b], _always)
    assert t.rows == 1 and _grid(t)[(0, 1)][2] == "앞 글\n뒤 글"


def _split_parts(last_line_r):
    """두 쪽 조각: 앞 조각 끝 행 오른쪽 칸 글이 쪽 끝에서 끊겼는지(last_line_r) 보는 표. 쪽 경계엔 괘선이 있다."""
    xs = [100.0, 300.0, 500.0]
    u = PCell(1, 1, 1, 1, "가나다 라마바 사아자", lines=[(120, 310, 490, 10), (105, 310, last_line_r, 10)],
              ltext=["가나다 라마바", "사아자"])
    a = _part(1, 700, 60, xs, [PCell(0, 0, 1, 1, "구분", lines=[(680, 110, 140, 10)], ltext=["구분"]),
                               PCell(0, 1, 1, 1, "내용", lines=[(680, 310, 340, 10)], ltext=["내용"]),
                               PCell(1, 0, 1, 1, "갑", lines=[(130, 110, 125, 10)], ltext=["갑"]), u],
              RowRules(True, True, 3, 0))
    b = _part(2, 760, 600, xs, [PCell(0, 0, 1, 1, ""), PCell(0, 1, 1, 1, "차카타", lines=[(740, 310, 400, 10)], ltext=["차카타"]),
                                PCell(1, 0, 1, 1, "을", lines=[(700, 110, 125, 10)], ltext=["을"]),
                                PCell(1, 1, 1, 1, "다음 행", lines=[(700, 310, 380, 10)], ltext=["다음 행"])],
              RowRules(True, True, 1, 0))
    return a, b


def test_split_row_merged_when_last_line_full():
    """앞 쪽 끝줄이 칸 오른끝까지 찼고(문단 중간에서 끊김) 뒤 쪽 첫 줄이 짧게 끝나면(왼쪽 정렬 증거) 한 행이 쪽에서 갈린 것 → 합친다."""
    (t,) = merge_parts(list(_split_parts(490)), _always)
    g = _grid(t)
    assert t.rows == 3 and g[(1, 1)][2].replace("\n", " ") == "가나다 라마바 사아자 차카타" and g[(2, 0)][2] == "을"


def test_new_row_kept_when_last_line_short():
    """앞 쪽 끝줄이 짧게 끝났으면(문단 끝) 뒤 쪽 첫 행은 새 행이다."""
    (t,) = merge_parts(list(_split_parts(350)), _always)
    assert t.rows == 4
