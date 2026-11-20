# -*- coding: utf-8 -*-
"""HWPX(ZIP + OWPML XML) → 블록 목록 (문서 순서 유지).

주의(실험 준비 중 발견): <hp:t> 안에 <hp:tab/>·<hp:lineBreak/> 같은 표시가 끼면 그 뒤 글자는 표시의 tail에 있다.
보안: 외부 엔티티를 풀지 않는 파서로 읽는다(XXE 방지), 압축 해제 크기 상한을 둔다(압축 폭탄 방지).
"""
import io
import re
import zipfile

from lxml import etree

MAX_UNZIPPED = 200 * 1024 * 1024  # 200MB
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
INLINE = {"tab": "\t", "lineBreak": "\n", "fwSpace": " ", "nbSpace": " ", "hyphen": "-"}


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _t_text(t_el):
    out = [t_el.text or ""]
    for ch in t_el:
        out.append(INLINE.get(_local(ch.tag), ""))
        out.append(ch.tail or "")
    return "".join(out)


def _all_text(el):
    return "".join(_t_text(e) for e in el.iter() if _local(e.tag) == "t")


def _para_text(p):
    out = []
    for run in p:
        if _local(run.tag) != "run":
            continue
        for e in run:
            if _local(e.tag) == "t":
                out.append(_t_text(e))
            elif _local(e.tag) == "tab":
                out.append("\t")
    return "".join(out).strip()


def _xml(z, name):
    return etree.fromstring(z.read(name), _PARSER)


def is_encrypted(z: zipfile.ZipFile) -> bool:
    try:
        man = z.read("META-INF/manifest.xml")
    except KeyError:
        return False
    return b"encryption-data" in man or b"EncryptionData" in man


def read_hwpx(data: bytes) -> dict:
    z = zipfile.ZipFile(io.BytesIO(data))
    if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
        raise ValueError("압축 해제 크기 상한 초과(압축 폭탄 의심)")
    if is_encrypted(z):
        return {"status": "encrypted", "blocks": []}
    header = _xml(z, "Contents/header.xml")
    outline = {}
    for s in header.iter():
        if _local(s.tag) == "style":
            m = re.fullmatch(r"(?:개요|제목|heading)\s*(\d+)", (s.get("name") or "").strip(), re.I)
            if m:
                outline[s.get("id")] = int(m.group(1))
    blocks = []

    def table_block(tbl):
        cells = []
        for tr in tbl:
            if _local(tr.tag) != "tr":
                continue
            for tc in tr:
                if _local(tc.tag) != "tc":
                    continue
                addr = next(e for e in tc if _local(e.tag) == "cellAddr")
                span = next(e for e in tc if _local(e.tag) == "cellSpan")
                sub = next((e for e in tc if _local(e.tag) == "subList"), tc)
                cells.append((int(addr.get("rowAddr")), int(addr.get("colAddr")), int(span.get("rowSpan")),
                               int(span.get("colSpan")), re.sub(r"\s+", " ", _all_text(sub)).strip()))
        return {"type": "table", "rows": int(tbl.get("rowCnt")), "cols": int(tbl.get("colCnt")), "cells": sorted(cells)}

    def walk(el, where="body"):
        for ch in el:
            t = _local(ch.tag)
            if t in ("header", "footer", "footNote", "endNote"):
                kind = {"header": "header", "footer": "footer"}.get(t, "footnote")
                txt = _all_text(ch).strip()
                if txt:
                    blocks.append({"type": "furniture", "kind": kind, "text": txt})
                continue  # 머리말·각주 안의 표·문단은 본문으로 내지 않는다
            if t == "tbl":
                if where == "body":
                    blocks.append(table_block(ch))
                continue  # 칸 안의 중첩 표 글자는 칸 글자에 이미 들어 있다
            if t == "p" and where == "body":
                txt = _para_text(ch)
                if txt:
                    blocks.append({"type": "para", "text": txt, "style_level": outline.get(ch.get("styleIDRef"))})
            walk(ch, where)

    for name in sorted((n for n in z.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)),
                       key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[-1]).group(1))):
        walk(_xml(z, name))
    return {"status": "ok", "blocks": blocks}
