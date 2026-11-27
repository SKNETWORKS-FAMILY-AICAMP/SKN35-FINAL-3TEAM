# -*- coding: utf-8 -*-
"""HWPML(한글 XML, HWPX 이전 세대) → 블록 목록 (문서 순서 유지). 블록 모양은 hwpx.py 와 같다.

왜 필요한가
  법제처에서 받은 법령 '.hwp' 는 실제로는 바이너리 HWP 가 아니라 HWPML(XML) 이다 (router 가 내용으로 판별).
  이전에는 "HWPML(XML) 리더 없음"으로 거절했다 → 규제 산업의 핵심 문서인 법령·감독규정을 못 읽었다.
구조 (HWPX 와 거의 같고 태그 이름만 다르다)
  HEAD/…/STYLE Id Name="개요 1"            → 제목 단계
  BODY/SECTION/P[Style] /TEXT/CHAR        → 문단 글자 (CHAR 안에 TAB·LINEBREAK 같은 표시가 끼면 뒤 글자는 tail)
  …/TEXT/TABLE RowCount ColCount /ROW/CELL ColAddr RowAddr ColSpan RowSpan /PARALIST/P → 표
  …/TEXT/HEADER·FOOTER·FOOTNOTE·ENDNOTE   → 머리말·꼬리말·각주 (본문으로 내지 않는다)
보안
  파일 안 DOCTYPE(내부 DTD) 은 통째로 지우고, 거기서 쓰던 이름 붙은 글자(&nbsp; 등)만 숫자 참조로 바꾼다
  → 외부 엔티티·엔티티 폭탄을 원천 차단한다. 크기 상한을 둔다.
"""
import re

from lxml import etree

MAX_BYTES = 200 * 1024 * 1024
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, load_dtd=False)
INLINE = {"TAB": "\t", "LINEBREAK": "\n", "FWSPACE": " ", "NBSPACE": " ", "HYPHEN": "-"}
FURN = {"HEADER": "header", "FOOTER": "footer", "FOOTNOTE": "footnote", "ENDNOTE": "footnote"}
_DOCTYPE = re.compile(r"<!DOCTYPE[^\[>]*(\[.*?\])?\s*>", re.S)
_NAMED = {"nbsp": "&#160;", "lt": "&lt;", "gt": "&gt;", "amp": "&amp;", "quot": "&quot;", "apos": "&apos;"}


def _clean(data: bytes) -> bytes:
    """DOCTYPE 를 지우고 이름 붙은 글자를 숫자 참조로 바꾼 UTF-8 바이트. UTF-16(BOM) 파일도 UTF-8 로 맞춘다."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        text = data.decode("utf-8", errors="replace")
    text = re.sub(r"^﻿", "", text)
    text = re.sub(r"<\?xml[^>]*\?>", '<?xml version="1.0" encoding="utf-8"?>', text, count=1)
    text = _DOCTYPE.sub("", text, count=1)
    text = re.sub(r"&([A-Za-z]+);", lambda m: _NAMED.get(m.group(1), "�"), text)   # 모르는 이름은 대체 문자
    return text.encode("utf-8")


def _char_text(ch):
    """CHAR 하나의 글자 (안에 낀 TAB·LINEBREAK 표시 포함)."""
    out = [ch.text or ""]
    for e in ch:
        out.append(INLINE.get(e.tag, ""))
        out.append(e.tail or "")
    return "".join(out)


def _all_text(el):
    """요소 아래 모든 CHAR 글자 (칸·머리말 글자용)."""
    return "".join(_char_text(e) for e in el.iter("CHAR"))


def _para_text(p):
    """P 바로 아래 TEXT 의 CHAR 만 (표·머리말 같은 컨트롤 안 글자는 빼고)."""
    return "".join(_char_text(ch) for t in p if t.tag == "TEXT" for ch in t if ch.tag == "CHAR").strip()


def read_hwpml(data: bytes) -> dict:
    if len(data) > MAX_BYTES:
        raise ValueError("HWPML 크기 상한 초과")
    root = etree.fromstring(_clean(data), _PARSER)
    if root.tag != "HWPML":
        return {"status": "failed", "blocks": [], "reason": f"HWPML 루트가 아님: {root.tag}"}
    # 제목 단계: 스타일 이름이 '개요 n'·'제목 n'·'heading n' 인 것 (hwpx.py 와 같은 규칙)
    outline = {}
    for s in root.iter("STYLE"):
        m = re.fullmatch(r"(?:개요|제목|heading)\s*(\d+)", (s.get("Name") or "").strip(), re.I)
        if m:
            outline[s.get("Id")] = int(m.group(1))
    blocks = []

    def table_block(tbl):
        cells = []
        for row in tbl.iter("ROW"):
            if row.getparent() is not tbl:          # 칸 안 중첩 표의 행은 건너뛴다 (글자는 바깥 칸에 들어 있다)
                continue
            for cell in row:
                if cell.tag != "CELL":
                    continue
                cells.append((int(cell.get("RowAddr")), int(cell.get("ColAddr")), int(cell.get("RowSpan", 1)),
                              int(cell.get("ColSpan", 1)), re.sub(r"\s+", " ", _all_text(cell)).strip()))
        return {"type": "table", "rows": int(tbl.get("RowCount")), "cols": int(tbl.get("ColCount")), "cells": sorted(cells)}

    def walk_para(p):
        """문단 하나: 먼저 문단 글자, 그다음 그 문단에 든 컨트롤(표·머리말 등)을 순서대로."""
        txt = _para_text(p)
        if txt:
            blocks.append({"type": "para", "text": txt, "style_level": outline.get(p.get("Style"))})
        for t in p:
            if t.tag != "TEXT":
                continue
            for c in t:
                if c.tag in FURN:
                    ftxt = _all_text(c).strip()
                    if ftxt:
                        blocks.append({"type": "furniture", "kind": FURN[c.tag], "text": ftxt})
                elif c.tag == "TABLE":
                    blocks.append(table_block(c))
                elif c.tag != "CHAR":
                    walk_container(c)      # 글상자·그림 등: 안에 든 문단을 본문으로 (hwpx.py 의 walk 와 같은 동작)

    def walk_container(el):
        """컨트롤 안을 따라 내려가며 P 를 만나면 문단으로 처리한다. 그 안의 표·머리말은 walk_para 가 다룬다."""
        for ch in el:
            if ch.tag == "P":
                walk_para(ch)
            elif ch.tag not in FURN and ch.tag != "TABLE":
                walk_container(ch)

    for body in root.iter("BODY"):
        for sec in body.iter("SECTION"):
            for p in sec:
                if p.tag == "P":
                    walk_para(p)
    return {"status": "ok", "blocks": blocks}
