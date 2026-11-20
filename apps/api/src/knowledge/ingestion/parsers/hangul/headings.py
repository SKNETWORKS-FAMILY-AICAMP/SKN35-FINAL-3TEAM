# -*- coding: utf-8 -*-
"""제목 규칙: 문단 블록 중 제목을 찾아 단계(level)를 매긴다.

우선순위
1. 개요/제목 스타일이 실제로 쓰였으면 그 단계(정확)
2. 없으면 번호 패턴
   - 법령형(제n장·제n조가 3개 이상): 편 > 장 > 절 > 관 > 조, 부칙·별표·별지는 장과 같은 높이
   - 공문·보고서형: 로마숫자 / 붙임 > 1. > 가.  (짧고 문장으로 끝나지 않는 줄만)
   단계는 "열려 있는 상위 제목 수 + 1"로 매긴다 → 절이 없는 장 아래 조는 2단계, 절 아래 조는 3단계.
3. 제목이 2개 이상인 구조 문서면, 첫 문단이 짧고 패턴이 없을 때 문서 제목(title)으로 본다.

실험 1에서 나온 문제 처리
- 조문 줄 "제1조(목적) 이 법은 …" → 제목 "제1조(목적)" + 본문 문단으로 나눈다.
- 제목 끝의 개정 꼬리표 "<개정 2014. 3. 24.>"·"[본조신설 …]"는 제목에서 떼어 바로 뒤 본문으로 둔다(글자는 버리지 않는다).
"""
import re

LAW = [
    ("편", 1, re.compile(r"^제\s*\d+\s*편(\s|$)")),
    ("장", 2, re.compile(r"^제\s*\d+\s*장(의\s*\d+)?(\s|$)")),
    ("절", 3, re.compile(r"^제\s*\d+\s*절(의\s*\d+)?(\s|$)")),
    ("관", 4, re.compile(r"^제\s*\d+\s*관(\s|$)")),
    ("조", 5, re.compile(r"^제\s*\d+\s*조(의\s*\d+)?\s*\(")),
    ("부칙", 2, re.compile(r"^부\s*칙(\s|<|\(|$)")),
    ("별표", 2, re.compile(r"^\[?\s*별\s*표\s*\d")),
    ("별지", 2, re.compile(r"^\[?\s*별지\s*제?\s*\d")),
]
MEMO = [
    ("로마", 1, re.compile(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]\s*[.．]")),
    ("붙임", 1, re.compile(r"^붙임\s*\d+\s*[.．]?\s*\S")),
    ("숫자", 2, re.compile(r"^\d{1,2}\s*[.．]\s*\S")),
    ("가나", 3, re.compile(r"^[가-하]\s*[.．]\s*\S")),
]
SENTENCE_END = re.compile(r"(다|요|함|됨|음|임)\s*[.．]\s*$|다\s*$|끝\s*[.．]\s*$")
ARTICLE = re.compile(r"^(제\s*\d+\s*조(?:의\s*\d+)?\s*\([^)]*\))\s*(.*)$", re.S)
REV_TAG = re.compile(r"\s*((?:<[^<>]{2,60}>|\[[^\[\]]{2,60}\])\s*)+$")


def _kind(text, law_doc):
    for kind, rank, rx in LAW:
        if rx.search(text):
            return kind, rank
    if law_doc:
        return None
    if len(text) <= 40 and not SENTENCE_END.search(text):
        for kind, rank, rx in MEMO:
            if rx.search(text):
                return kind, rank
    return None


def assign(blocks: list) -> list:
    paras = [b for b in blocks if b["type"] == "para"]
    use_style = any(b.get("style_level") for b in paras)
    law_doc = sum(1 for b in paras if any(rx.search(b["text"]) for k, _, rx in LAW if k in ("장", "조"))) >= 3
    out, stack, n_head = [], [], 0
    for b in blocks:
        if b["type"] != "para":
            out.append(b)
            continue
        text = b["text"]
        if use_style:
            if b.get("style_level"):
                out.append({"type": "heading", "text": text, "level": b["style_level"], "source": "style"})
                n_head += 1
            else:
                out.append(b)
            continue
        k = _kind(text, law_doc)
        if not k:
            out.append(b)
            continue
        kind, rank = k
        while stack and stack[-1] >= rank:
            stack.pop()
        stack.append(rank)
        head, body = text, ""
        if kind == "조":
            m = ARTICLE.match(text)
            if m:
                head, body = m.group(1), m.group(2).strip()
        tag = REV_TAG.search(head)
        if tag and tag.start() > 0:
            body = (head[tag.start():].strip() + " " + body).strip()
            head = head[: tag.start()].strip()
        out.append({"type": "heading", "text": head, "level": len(stack), "source": f"pattern:{kind}"})
        n_head += 1
        if body:
            out.append({"type": "para", "text": body, "style_level": None})
    # 문서 제목: 구조 문서(제목 2개 이상)에서 첫 블록이 짧은 문단이면
    if n_head >= 2:
        for i, b in enumerate(out):
            if b["type"] == "para" and len(b["text"]) <= 60 and not SENTENCE_END.search(b["text"]):
                out[i] = {"type": "title", "text": b["text"]}
            break
    return out
