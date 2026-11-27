# -*- coding: utf-8 -*-
"""HWP 5.x(바이너리, OLE) → 블록 목록. 실제 읽기는 syhwp 0.0.8(MIT)에 맡긴다(실험 5 결정).

암호·배포용 여부는 syhwp를 부르기 전에 FileHeader 속성 비트로 먼저 확인한다.
  bit0 압축, bit1 암호 설정, bit2 배포용 문서
"""
import contextvars
import io
import os
import struct
import tempfile

import olefile

# ------------------------------------------------------------------ 머리말·꼬리말·각주 구분
# syhwp 0.0.8 은 머리말·꼬리말·각주·미주 컨트롤 안 문단을 본문 문단과 똑같이 blocks 에 넣는다(_hwp5._emit_control).
# 그래서 "…내부 규정 | 대외비" 같은 머리말이 본문에 섞였다(S1, 2026-11-27 진단). HWPX 경로는 이것을 furniture 로 뺀다.
# syhwp 의 _emit_control 을 감싸서, 이 네 컨트롤이면 안의 blocks 를 표시(_Furniture)로 묶는다.
# 감싼 동작은 우리 read_hwp 안에서만 켜진다(contextvars) — 같은 프로세스의 다른 syhwp 사용에는 영향 없음.
# syhwp 내부 함수에 기대므로 버전은 0.0.8 로 고정한다(pyproject). 내부가 바뀌면 감싸기를 건너뛰고 예전처럼 동작한다.
_FURN_IDS = {b"head"[::-1]: "header", b"foot"[::-1]: "footer", b"fn  "[::-1]: "footnote", b"en  "[::-1]: "footnote"}
_COLLECT = contextvars.ContextVar("hwp_collect_furniture", default=False)


class _Furniture:
    """머리말·각주 컨트롤 하나가 낸 syhwp blocks 묶음."""

    def __init__(self, kind, blocks):
        self.kind, self.blocks = kind, blocks


def _install_furniture_hook() -> bool:
    try:
        from syhwp import _hwp5
    except ImportError:
        return False
    orig = getattr(_hwp5, "_emit_control", None)
    if orig is None or getattr(orig, "_furniture_hook", False):
        return orig is not None

    def emit_control(ctrl, blocks, depth=0):
        kind = _FURN_IDS.get(bytes(ctrl.payload[:4])) if _COLLECT.get() and len(ctrl.payload) >= 4 else None
        if kind is None:
            return orig(ctrl, blocks, depth)
        inner = []
        orig(ctrl, inner, depth)              # 원래 동작 그대로 안쪽 blocks 를 만들고
        if inner:
            blocks.append(_Furniture(kind, inner))   # 본문 대신 묶음 하나로 넣는다

    emit_control._furniture_hook = True
    _hwp5._emit_control = emit_control
    return True


def _block_text(b) -> str:
    """syhwp block 하나의 글 (머리말 묶음 글자 만들기용)."""
    name = type(b).__name__
    if name == "Paragraph":
        return _fix_text(b.text)
    if name == "Table":
        return " ".join(_fix_text(c.text) for c in b.cells)
    return ""


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
    hooked = _install_furniture_hook()
    token = _COLLECT.set(hooked)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        d = syhwp.open(tmp)
    finally:
        _COLLECT.reset(token)
        os.remove(tmp)
    blocks = []
    for b in d.blocks:
        name = type(b).__name__
        if isinstance(b, _Furniture):          # 머리말·꼬리말·각주 → 본문이 아닌 영역 (hwpx.py 와 같은 블록)
            text = " ".join(t for t in (" ".join(_block_text(x).split()) for x in b.blocks) if t).strip()
            if text:
                blocks.append({"type": "furniture", "kind": b.kind, "text": text})
        elif name == "Paragraph":
            text = _fix_text(b.text)
            if text.strip():
                blocks.append({"type": "para", "text": text.strip(), "style_level": None})
        elif name == "Table":
            cells = sorted((c.row, c.col, c.row_span, c.col_span, " ".join(_fix_text(c.text).split())) for c in b.cells)
            blocks.append({"type": "table", "rows": b.n_rows, "cols": b.n_cols, "cells": cells})
        elif name == "Equation":
            blocks.append({"type": "para", "text": f"[수식: {_fix_text(b.script)}]", "style_level": None})
    return {"status": "ok", "blocks": blocks, "flags": flags}
