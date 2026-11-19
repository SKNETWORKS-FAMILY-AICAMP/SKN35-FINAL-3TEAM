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
            if b.text.strip():
                blocks.append({"type": "para", "text": b.text.strip(), "style_level": None})
        elif name == "Table":
            cells = sorted((c.row, c.col, c.row_span, c.col_span, " ".join(c.text.split())) for c in b.cells)
            blocks.append({"type": "table", "rows": b.n_rows, "cols": b.n_cols, "cells": cells})
        elif name == "Equation":
            blocks.append({"type": "para", "text": f"[수식: {b.script}]", "style_level": None})
    return {"status": "ok", "blocks": blocks, "flags": flags}
