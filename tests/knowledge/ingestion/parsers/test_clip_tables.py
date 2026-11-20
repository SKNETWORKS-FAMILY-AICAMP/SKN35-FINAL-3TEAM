# -*- coding: utf-8 -*-
"""한컴 PDF 칸 클립 → 표 격자(clip_tables, kordoc clip-cells 이식) 단위 테스트."""
from knowledge.ingestion.parsers.pdf.clip_tables import build_clip_grids
from knowledge.ingestion.parsers.pdf.pdf_graphics import PageGraphics, Rect, read_page_graphics

W, H = 600.0, 800.0


def _grid(clips, **kw):
    g = PageGraphics(width=W, height=H, clips=clips, **kw)
    grids, _, _ = build_clip_grids(g, [])
    return grids


def test_merged_cell_from_clips():
    """2x3 표: 왼쪽 열이 두 행에 걸친 병합 칸 (클립 하나), 나머지 1칸씩 — 클립이 맞닿으면 한 표."""
    clips = [Rect(100, 500, 200, 600),                       # 병합 칸 (두 행)
             Rect(200, 550, 300, 600), Rect(300, 550, 400, 600),
             Rect(200, 500, 300, 550), Rect(300, 500, 400, 550)]
    (gr,) = _grid(clips)
    assert (len(gr.rowYs) - 1, len(gr.colXs) - 1) == (2, 3)
    big = next(c for c in gr.cells if c.col == 0 and not c.filler)
    assert (big.row, big.rowspan, big.colspan) == (0, 2, 1)


def test_repeated_clip_is_deduplicated_and_lone_clip_is_not_table():
    """같은 사각형이 문단마다 반복 클립돼도 한 칸. 이웃 없는 단독 클립(글상자)은 표가 아니다."""
    clips = [Rect(100, 500, 200, 550), Rect(100, 500, 200, 550), Rect(200, 500, 300, 550),
             Rect(400, 100, 500, 150)]
    grids = _grid(clips)
    assert len(grids) == 1 and len([c for c in grids[0].cells if not c.filler]) == 2


def test_invisible_column_split_kept():
    """원본 칸 경계가 0.5pt만 달라도 다른 열로 선다(보이지 않는 칸 쪼개기 — HWPX 격자와 같게)."""
    clips = [Rect(100, 550, 200, 600), Rect(200, 550, 300, 600),
             Rect(100, 500, 200.5, 550), Rect(200.5, 500, 300, 550)]
    (gr,) = _grid(clips)
    assert len(gr.colXs) - 1 == 3


def test_clip_rects_read_from_content_stream(tmp_path):
    """내용 스트림의 '경로 + W* n' 클립과 '획(S)' 괘선을 읽는다 (한컴 PDF 모양). pypdf가 없는 환경에서는 건너뛴다."""
    import pytest
    pytest.importorskip("pypdf")
    content = (b"q 100 500 m 200 500 l 200 600 l 100 600 l h W* n "
               b"0 0 1 rg BT ET Q "
               b"100 500 m 300 500 l S")
    pdf = _minimal_pdf(content)
    p = tmp_path / "t.pdf"
    p.write_bytes(pdf)
    g = read_page_graphics(p)[1]
    assert len(g.clips) == 1 and (g.clips[0].x1, g.clips[0].y2) == (100, 600)
    assert len(g.horizontals) == 1 and g.horizontals[0].x2 == 300


def _minimal_pdf(content: bytes) -> bytes:
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Contents 4 0 R /Resources << >> >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def test_word_straddling_cell_edge_split_by_char_positions():
    """칸 경계(x=130)를 가로지르는 낱말 "변동△0.09"를 글자 위치로 "변동" | "△0.09" 로 나눈다. 글자 수가 다르면 그대로."""
    from knowledge.ingestion.parsers.pdf.clip_tables import ClipGrid, GridCell, split_straddling_words
    H = 800.0
    gr = ClipGrid([500, 480], [100, 130, 200], Rect(100, 480, 200, 500),
                  [GridCell(0, 0, 1, 1, Rect(100, 480, 130, 500)), GridCell(0, 1, 1, 1, Rect(130, 480, 200, 500))])
    w = ("변동△0.09", 110, 305, 160, 315)
    chars = [("변", 110, 305, 118, 315), ("동", 119, 305, 127, 315), ("△", 133, 305, 141, 315),
             ("0", 142, 305, 146, 315), (".", 146, 305, 148, 315), ("0", 148, 305, 152, 315), ("9", 152, 305, 156, 315)]
    out = split_straddling_words([w], [gr], H, lambda: chars)
    assert [x[0] for x in out] == ["변동", "△0.09"]
    assert split_straddling_words([w], [gr], H, lambda: chars[:3]) == [w]
