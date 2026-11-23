# -*- coding: utf-8 -*-
"""구조 기반 청커 함정 테스트 (2차 청킹 실험 test_traps.py 25개 + C2 새 기능 10개를 옮김). 가상 글만 쓴다.

길이 세기: 환경변수 CHUNKER_TOKENIZER_PATH가 있으면 실제 KURE-v3 토크나이저, 없으면 글자 수(파일 없이 어디서나 돌게).
상한(cap)은 길이 세기 방식에 맞춰 고른다(같은 글이 나뉘는지·안 나뉘는지가 시험 조건이라서).
"""
import os

import pytest

from knowledge.ingestion.chunkers import chunk_doc, token_counter
from knowledge.ingestion.chunkers.doc import Doc, norm

REAL = bool(os.environ.get("CHUNKER_TOKENIZER_PATH"))
COUNT = token_counter() if REAL else (lambda t: len(t) if t else 0)
# (기본 상한, 항 나누기 시험 상한, 표 나누기 시험 상한)
CAP, CAP_ITEM, CAP_TABLE = (512, 160, 140) if REAL else (900, 260, 220)

LONG = "이 규정은 회사의 정보보호에 관한 기본 사항을 정하여 정보자산을 안전하게 관리하는 것을 목적으로 한다. "


def el(t, text, marker=""):
    d = {"type": t, "text": text, "page": 1, "pages": [1], "bbox": None, "heading_path": []}
    if marker:
        d["marker"] = marker
    return d


def tbl(cells, nr, nc):
    return {"type": "table", "text": " ".join(c[4] for c in cells), "page": 1, "pages": [1], "bbox": None, "heading_path": [],
            "table": {"rows": nr, "cols": nc, "source": "test",
                      "cells": [{"r": r, "c": c, "rs": rs, "cs": cs, "text": t, "h": False} for r, c, rs, cs, t in cells]}}


def parsed(name, elements):
    return {"status": "ok", "file_name": name, "doc_version_id": 1, "elements": elements}


def run(p, cap=None):
    return chunk_doc(p, max_tokens=cap or CAP, count_tokens=COUNT)


def assert_no_loss(p, chunks):
    """글 보존: 파서 글(공백 뺀 글)의 모든 글자가 청크 하나에만 들어감(빠짐 0, 중복 0)."""
    doc = Doc(p)
    cover = [0] * len(doc.G)
    for c in chunks:
        for a, b in doc.global_spans(c["source_spans"]):
            for k in range(a, b):
                cover[k] += 1
    assert cover.count(0) == 0, f"빠진 글자 {cover.count(0)}"
    assert all(x <= 1 for x in cover), f"중복 글자 {sum(x - 1 for x in cover if x > 1)}"


def law_base():
    return [el("title", "가상 정보보호 규정"),
            el("section_header", "제1장 총칙"),
            el("text", "제1조(목적) " + LONG),
            el("text", "제2조(정의) 이 규정에서 사용하는 용어의 뜻은 다음과 같다."),
            el("list_item", "“정보자산”이란 회사가 보유한 정보를 말한다.", "1."),
            el("list_item", "“사용자”란 정보자산을 쓰는 사람을 말한다.", "2.")]


# ---------------------------------------------------------------- 함정 1·5: 본문 앞머리의 조 제목, "제3조(책임) ①"
def test_trap1_5_article_heading_at_paragraph_start():
    base = law_base()
    base[2] = el("text", "제1조(목적) " + LONG * 3)
    p = parsed("t1", base + [el("text", "제3조(책임) ① 사용자는 정보자산을 보호하여야 한다. " + LONG * 2 + "② 관리자는 이를 감독한다. " + LONG)])
    c = run(p)
    paths = [ch["heading_path"] for ch in c]
    assert any(x and x[-1].startswith("제1조(목적)") for x in paths), paths
    assert any("제3조(책임)" in " ".join(x) for x in paths), paths
    assert all("제1장 총칙" in x for x in paths if x and x[-1].startswith("제")), paths
    assert_no_loss(p, c)


# ---------------------------------------------------------------- 함정 2: 본문 인용 "제3조에 따라"
def test_trap2_citation_is_not_heading():
    p = parsed("t2", law_base() + [el("text", "제3조(위임) 세부 사항은 따로 정한다."),
                                   el("text", "제3조에 따라 정한 세부 사항은 관리자가 공지한다.")])
    c = run(p)
    assert not any(x and x[-1].startswith("제3조에") for x in [ch["heading_path"] for ch in c])
    assert_no_loss(p, c)


# ---------------------------------------------------------------- 함정 3: "1.5배", "서식을 제출한다"
def test_trap3_number_like_text_is_not_heading():
    p = parsed("t3", [el("title", "가상 보고서"), el("section_header", "1. 개요"),
                      el("text", "1.5배 늘어난 예산을 반영한다."), el("text", "서식을 제출한다. 담당자는 확인한다."),
                      el("section_header", "2. 결과"), el("text", "결과는 다음과 같다.")])
    c = run(p)
    hp = [x for ch in c for x in ch["heading_path"]]
    assert not any(x.startswith("1.5") or x.startswith("서식") for x in hp), hp
    assert_no_loss(p, c)


# ---------------------------------------------------------------- 함정 4: ①②③이 한 문단 + "①에 따른" 인용
def test_trap4_split_at_items_not_at_citation():
    para = ("제5조(점검) ① 관리자는 매년 정보자산을 점검하여야 한다. " + LONG * 2 +
            "② 제1항의 점검 결과는 보고하여야 한다. ①에 따른 점검 계획은 미리 세운다. " + LONG * 2 +
            "③ 점검 기록은 3년간 보관한다. " + LONG * 2)
    p = parsed("t4", law_base() + [el("text", para)])
    c = run(p, CAP_ITEM)
    starts = [norm(ch["content"])[:2] for ch in c if "제5조" in " ".join(ch["heading_path"])]
    assert any(s.startswith("②") for s in starts) and any(s.startswith("③") for s in starts), starts
    assert not any(s.startswith("①에") for s in starts), starts
    assert_no_loss(p, c)


# ---------------------------------------------------------------- 함정 7·9: 두 줄 머리행 표, 같은 값 이웃 칸
def test_trap7_9_split_table_repeats_two_header_rows():
    cells = [(0, 0, 2, 1, "대분류"), (0, 1, 1, 2, "보안등급"), (1, 1, 1, 1, "기밀성"), (1, 2, 1, 1, "무결성")]
    for r in range(2, 26):
        cells += [(r, 0, 1, 1, f"정보자산{r}"), (r, 1, 1, 1, "상"), (r, 2, 1, 1, "상")]
    p = parsed("t7", [el("title", "가상 점검표"), el("section_header", "1. 점검 기준"), el("text", "기준은 다음과 같다:"), tbl(cells, 26, 3)])
    c = run(p, CAP_TABLE)
    parts = [ch for ch in c if ch["kind"] == "표"]
    assert len(parts) >= 2, f"표 조각 {len(parts)}"
    assert all(all(x in ch["content"] for x in ("대분류", "보안등급", "기밀성", "무결성")) for ch in parts)
    assert "기준은 다음과 같다" in parts[0]["content"]
    assert any("기밀성: 상" in ch["embed_text"] and "무결성: 상" in ch["embed_text"] for ch in parts)
    assert [ch["table_part"] for ch in parts] == [f"{i}/{len(parts)}" for i in range(1, len(parts) + 1)]
    assert_no_loss(p, c)


# ---------------------------------------------------------------- 함정 8: "항목: 값" 2열 표
def test_trap8_two_column_label_table():
    cells = [(0, 0, 1, 1, "성명"), (0, 1, 1, 1, "가상일"), (1, 0, 1, 1, "소속"), (1, 1, 1, 1, "정보보호팀"),
             (2, 0, 1, 1, "연락처"), (2, 1, 1, 1, "000-0000")]
    p = parsed("t8", [el("title", "가상 신청서"), el("section_header", "1. 신청인"), tbl(cells, 3, 2)])
    c = run(p)
    e = " ".join(ch["embed_text"] for ch in c)
    assert "성명: 가상일" in e and "소속: 정보보호팀" in e and "성명: 소속" not in e, e[:200]
    assert_no_loss(p, c)


# ---------------------------------------------------------------- 함정 13: 내용 없는 제목
def test_trap13_empty_heading_stays_in_path():
    p = parsed("t13", law_base())
    c = run(p)
    assert not any(norm(ch["content"]) == norm("제1장 총칙") for ch in c)
    assert any("제1장 총칙" in ch["content"] for ch in c)
    assert_no_loss(p, c)


# ---------------------------------------------------------------- 함정 14: 짧은 조 합치기
def test_trap14_merge_short_articles_keeps_titles():
    p = parsed("t14", [el("title", "가상 규정"), el("section_header", "제1장 총칙"),
                       el("text", "제1조(목적) 짧다."), el("text", "제2조(적용) 짧다."), el("text", "제3조(시행) 짧다.")])
    c = run(p)
    merged = [ch for ch in c if ch["heading_path"] and ch["heading_path"][-1] == "제1장 총칙" and "제2조(적용)" in ch["content"]]
    assert merged, [ch["heading_path"] for ch in c]
    assert "제1조(목적)" in merged[0]["content"] or "제1조(목적)" in " ".join(merged[0]["heading_path"])
    assert_no_loss(p, c)


# ---------------------------------------------------------------- C2 ①②: 목차 닫기, 하이픈·질문 번호
def test_c2_toc_closed_and_hyphen_question_numbers():
    p = parsed("c2_toc", [el("title", "가상 업무 Q&A"),
                          el("section_header", "목 차"),
                          el("document_index", "1. 대출 안내 ··········· 1"),
                          el("text", "2. 연체 관리 ··········· 3"),
                          el("section_header", "1. 대출 안내"),
                          el("section_header", "1-1. 대출 신청 방법"), el("text", LONG * 2),
                          el("section_header", "1-2. 대출 한도"), el("text", LONG * 2),
                          el("section_header", "2. 연체 관리"),
                          el("section_header", "Q1 연체 알림은 언제 보내나요?"), el("text", LONG * 2),
                          el("section_header", "Q2 연체 이자는?"), el("text", LONG * 2)])
    c = run(p)
    P = {ch["heading_path"][-1]: ch["heading_path"] for ch in c if ch["heading_path"]}
    assert all("목 차" not in x for k, x in P.items() if k != "목 차"), P
    assert P.get("1-1. 대출 신청 방법") == ["1. 대출 안내", "1-1. 대출 신청 방법"]
    assert P.get("1-2. 대출 한도") == ["1. 대출 안내", "1-2. 대출 한도"]
    assert P.get("Q1 연체 알림은 언제 보내나요?") == ["2. 연체 관리", "Q1 연체 알림은 언제 보내나요?"]
    assert P.get("Q2 연체 이자는?") == ["2. 연체 관리", "Q2 연체 이자는?"]
    assert not any("····" in x[-1] for x in P.values())
    assert_no_loss(p, c)


# ---------------------------------------------------------------- C2 ③: 나눌 곳 없는 덩어리 강제 자르기
def test_c2_unbreakable_blob_is_split_under_cap():
    p = parsed("c2_blob", [el("title", "가상 매뉴얼"), el("section_header", "1. 개요"), el("text", "․" * 6000), el("text", LONG)])
    c = run(p)
    assert all(COUNT(ch["content"]) <= CAP and COUNT(ch["embed_text"]) <= CAP for ch in c)
    assert_no_loss(p, c)


# ---------------------------------------------------------------- 상자 제목 형제 규칙 (C1 dev 수정 회귀)
def test_box_title_sibling_rule():
    p = parsed("c2_box", [el("title", "가상 공문"), el("section_header", "□ 추진 배경"), el("text", "1. 현황"), el("text", LONG * 2),
                          el("section_header", "□ 추진 계획"), el("text", LONG * 2)])
    c = run(p)
    assert ["□ 추진 계획"] in [ch["heading_path"] for ch in c]


# ---------------------------------------------------------------- 출력 형식 (인수인계 4장)
def test_output_fields_and_permissions():
    p = parsed("fmt", law_base())
    c = chunk_doc(p, max_tokens=CAP, count_tokens=COUNT,
                       doc_meta={"owner_dept_id": 7, "scope": "dept", "security_grade": 2})
    need = {"chunk_index", "doc_version_id", "content", "embed_text", "heading_path", "kind", "page", "pages",
            "table_part", "owner_dept_id", "scope", "security_grade", "source_spans"}
    assert c and all(need <= set(ch) for ch in c)
    assert [ch["chunk_index"] for ch in c] == list(range(len(c)))
    assert all(ch["embed_text"].startswith("가상 정보보호 규정") for ch in c)
    assert all((ch["owner_dept_id"], ch["scope"], ch["security_grade"]) == (7, "dept", 2) for ch in c)
    assert all(ch["doc_version_id"] == 1 for ch in c)


def test_not_ok_parse_gives_no_chunks():
    assert chunk_doc({"status": "failed", "elements": []}, count_tokens=COUNT) == []
