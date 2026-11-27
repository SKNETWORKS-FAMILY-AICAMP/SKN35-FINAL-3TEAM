# -*- coding: utf-8 -*-
"""HWPML 리더(parsers/hangul/hwpml.py) — 법제처 법령 '.hwp' 가 실제로 이 형식. 작은 HWPML 을 그 자리에서 만든다."""
from knowledge.ingestion.parsers.hangul.hwpml import read_hwpml
from knowledge.ingestion.parsers.pipeline import DocParser

HWPML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE HWPML [
\t<!ENTITY nbsp\t"&#160;">
\t<!ENTITY xxe SYSTEM "file:///C:/Windows/win.ini">
]>
<HWPML Version="2.1"><HEAD SecCnt="1"><MAPPINGTABLE><STYLELIST>
<STYLE Id="0" Type="Para" Name="바탕글"/><STYLE Id="2" Type="Para" Name="개요 1"/>
</STYLELIST></MAPPINGTABLE></HEAD>
<BODY><SECTION Id="0">
{법령집표지}
<P Style="0"><TEXT><HEADER><PARALIST><P><TEXT><CHAR>가나다 내부 규정</CHAR></TEXT></P></PARALIST></HEADER></TEXT></P>
<P Style="2"><TEXT><CHAR>제1장 총칙</CHAR></TEXT></P>
<P Style="0"><TEXT><CHAR>제1조(목적)&nbsp;이 규정은<TAB/>기준을 정한다.&xxe;</CHAR></TEXT></P>
<P Style="0"><TEXT><CHAR>아래 표와 같다.</CHAR><TABLE RowCount="2" ColCount="2">
<ROW><CELL ColAddr="0" RowAddr="0" ColSpan="2" RowSpan="1"><PARALIST><P><TEXT><CHAR>구분</CHAR></TEXT></P></PARALIST></CELL></ROW>
<ROW><CELL ColAddr="0" RowAddr="1" ColSpan="1" RowSpan="1"><PARALIST><P><TEXT><CHAR>담보</CHAR></TEXT></P></PARALIST></CELL>
<CELL ColAddr="1" RowAddr="1" ColSpan="1" RowSpan="1"><PARALIST><P><TEXT><CHAR>60%</CHAR></TEXT></P></PARALIST></CELL></ROW>
</TABLE></TEXT></P>
<P Style="0"><TEXT><CHAR>각주가 있는 문단</CHAR><FOOTNOTE><PARALIST><P><TEXT><CHAR>각주 글</CHAR></TEXT></P></PARALIST></FOOTNOTE></TEXT></P>
</SECTION></BODY></HWPML>"""


def test_blocks():
    r = read_hwpml(HWPML.encode("utf-8"))
    assert r["status"] == "ok"
    b = r["blocks"]
    assert b[0] == {"type": "furniture", "kind": "header", "text": "가나다 내부 규정"}       # 머리말은 본문 아님
    assert b[1] == {"type": "para", "text": "제1장 총칙", "style_level": 1}                 # 개요 1 스타일
    assert b[2]["text"] == "제1조(목적)\u00a0이 규정은\t기준을 정한다.\ufffd"                # &nbsp; → 글자, 외부 엔티티는 풀지 않음
    assert b[3]["text"] == "아래 표와 같다." and b[4]["type"] == "table"                     # 문단 글자 뒤에 표
    assert b[4]["cells"] == [(0, 0, 1, 2, "구분"), (1, 0, 1, 1, "담보"), (1, 1, 1, 1, "60%")]  # 가로 병합
    assert b[5]["text"] == "각주가 있는 문단" and b[6]["kind"] == "footnote"
    assert not any("법령집표지" in x.get("text", "") for x in b)                            # 문단 밖 틀 글자는 버린다
    assert not any("[fonts]" in x.get("text", "") or "for 16-bit" in x.get("text", "") for x in b)


def test_pipeline_reads_hwpml_named_hwp(tmp_path):
    f = tmp_path / "법령.hwp"                  # 법제처처럼 확장자는 .hwp
    f.write_text(HWPML, encoding="utf-8")
    r = DocParser().parse(f)
    assert r.status == "ok", r.reason
    assert r.route["format"] == "hwpml"
    assert "## 제1장 총칙" in r.markdown or "# 제1장 총칙" in r.markdown
    assert "가나다 내부 규정" not in r.markdown                                            # 머리말은 본문 마크다운에 없음
    table = next(e for e in r.elements if e["type"] == "table")
    assert table["table"]["source"] == "원본(HWPML)"
