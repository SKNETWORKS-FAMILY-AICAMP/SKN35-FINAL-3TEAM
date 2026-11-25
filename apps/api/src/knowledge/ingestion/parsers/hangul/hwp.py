# -*- coding: utf-8 -*-
"""HWP 5.x(바이너리, OLE) → 블록 목록. 실제 읽기는 syhwp 0.0.8(MIT)에 맡긴다(실험 5 결정).

암호·배포용 여부는 syhwp를 부르기 전에 FileHeader 속성 비트로 먼저 확인한다.
  bit0 압축, bit1 암호 설정, bit2 배포용 문서
"""
import io
import os
import struct
import tempfile

import olefile


def _fix_text(s: str) -> str:
    """syhwp 0.0.8은 HWP 글(UTF-16)을 한 칸(code unit)씩 chr()로 읽는다(syhwp/_hwp5.py).
    그래서 기본 다국어 평면 밖 글자(예: 한글 프로그램 전용 기호 U+F012B)가 짝 없는 서러게이트 두 칸으로 깨지고,
    이 글을 UTF-8로 저장하면 UnicodeEncodeError로 멈춘다. 여기서 두 칸을 다시 한 글자로 합친다.
    짝이 없는 칸이 남으면 U+FFFD로 바꾼다(저장이 멈추지 않게)."""
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in s):
        return s
    return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def header_flags(data: bytes) -> dict:
    ole = olefile.OleFileIO(io.BytesIO(data))
    h = ole.openstream("FileHeader").read()
    if not h.startswith(b"HWP Document File"):
        raise ValueError("HWP FileHeader 서명이 아님")
    props = struct.unpack("<I", h[36:40])[0]
    return {"encrypted": bool(props & 0b10), "distribution": bool(props & 0b100)}


def read_hwp(data: bytes, allow_distribution: bool = False) -> dict:
    flags = header_flags(data)
    if flags["encrypted"]:
        return {"status": "encrypted", "blocks": [], "flags": flags}
    if flags["distribution"] and not allow_distribution:
        return {"status": "distribution", "blocks": [], "flags": flags}
    try:
        import syhwp
    except ImportError:
        return {"status": "unsupported", "blocks": [], "flags": flags, "reason": "syhwp 미설치 — 바이너리 HWP 를 읽을 수 없음"}

    # syhwp는 경로를 받는다. 임시 파일은 시스템 임시 폴더(영어 경로)에 만들고 바로 지운다.
    fd, tmp = tempfile.mkstemp(suffix=".hwp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        d = syhwp.open(tmp)
    finally:
        os.remove(tmp)
    blocks = []
    for b in d.blocks:
        name = type(b).__name__
        if name == "Paragraph":
            text = _fix_text(b.text)
            if text.strip():
                blocks.append({"type": "para", "text": text.strip(), "style_level": None})
        elif name == "Table":
            cells = sorted((c.row, c.col, c.row_span, c.col_span, " ".join(_fix_text(c.text).split())) for c in b.cells)
            blocks.append({"type": "table", "rows": b.n_rows, "cols": b.n_cols, "cells": cells})
        elif name == "Equation":
            blocks.append({"type": "para", "text": f"[수식: {_fix_text(b.script)}]", "style_level": None})
    return {"status": "ok", "blocks": blocks, "flags": flags}
