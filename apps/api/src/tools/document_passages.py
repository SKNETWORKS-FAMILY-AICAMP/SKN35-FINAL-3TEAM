"""파싱된 첨부 문서 안에서만 문단을 고른다. DB나 외부 검색은 사용하지 않는다."""

from __future__ import annotations

import re
from collections import Counter


def query_terms(query: str) -> list[str]:
    words = re.findall(r"[가-힣A-Za-z0-9]+", query.casefold())
    stop = {"첨부", "문서", "파일", "내용", "알려", "알려줘", "주세요", "확인", "해줘", "가지", "두", "세", "각", "항목", "대한", "관련", "이번", "해서"}
    out = []
    for word in words:
        word = re.sub(r"(?:에서는|에서|으로|에는|와|과|의|을|를|은|는|이|가|도)$", "", word)
        if len(word) >= 2 and word not in stop and word not in out:
            out.append(word)
    return out


def passages(d: dict) -> list[dict]:
    """문서 순서·절 제목·표를 보존한 작은 읽기 단위. 큰 덩어리는 줄/문장 경계로 나눈다."""
    result = []
    heading = ""
    for element in d.get("elements") or []:
        text = (element.get("table") or {}).get("markdown") if "table" in element else element.get("text")
        if not text:
            continue
        if "table" in element:
            text = re.sub(r"[ \t]{2,}", " ", text)
            text = re.sub(r"-{4,}", "---", text)
        if element.get("type") in ("title", "section_header"):
            heading = text.strip()
        if element.get("type") in ("title", "section_header"):
            continue  # 제목은 위치로 붙이고, 빈 제목만 검색 결과를 차지하지 않게 한다.
        path = [str(p) for p in element.get("heading_path") or [] if p]
        location = " > ".join(path) or heading or (f"{element['page']}쪽" if element.get("page") else "본문")
        # 표는 행 순서와 헤더를 남긴다. 한 행이 매우 길면 분리해 범위 읽기로 접근 가능하게 한다.
        pieces = re.split(r"\n+|(?<=[.!?。])\s+", text)
        chunk = ""
        for piece in pieces:
            while len(piece) > 700:
                if chunk:
                    result.append({"location": location, "text": chunk})
                    chunk = ""
                result.append({"location": location, "text": piece[:700]})
                piece = piece[700:]
            if chunk and len(chunk) + len(piece) > 700:
                result.append({"location": location, "text": chunk})
                chunk = ""
            chunk += ("\n" if chunk else "") + piece
        if chunk:
            result.append({"location": location, "text": chunk})
    if not result:
        text = d.get("markdown") or ""
        result = [{"location": "본문", "text": text[i:i + 700]} for i in range(0, len(text), 700)]
    for index, item in enumerate(result):
        item["passage_id"] = index + 1
    return result


def select_passages(d: dict, *, query: str = "", section: str = "", offset: int = 0, char_offset: int = 0,
                    max_chars: int = 1800) -> tuple[list[dict], dict]:
    all_items = passages(d)
    candidates = [p for p in all_items if not section or section.casefold() in p["location"].casefold()]
    terms = query_terms(query)
    if terms:
        frequencies = Counter(t for p in candidates for t in terms if t in re.sub(r"\s+", "", p["location"] + p["text"]).casefold())
        remaining = list(candidates)
        ranked, covered = [], Counter()
        def score(p):
            body, title = re.sub(r"\s+", "", p["text"]).casefold(), re.sub(r"\s+", "", p["location"]).casefold()
            # 아직 다루지 않은 논점도 골라 앞부분의 절만 예산을 차지하지 않게 한다.
            return sum(((2 if t in body else 0) + (3 if t in title else 0)) /
                       ((1 + frequencies[t]) ** .5 * (1 + covered[t])) for t in terms)
        while remaining and len(ranked) < 64:
            best = max(remaining, key=score)
            if score(best) <= 0:
                break
            ranked.append(best)
            covered.update(t for t in terms if t in re.sub(r"\s+", "", best["text"]).casefold())
            remaining.remove(best)
        # 긴 문서의 모든 문단을 반복 재정렬하지 않는다. 뒷부분도 범위 읽기로 접근 가능하다.
        candidates = ranked + sorted((p for p in remaining if score(p) > 0), key=score, reverse=True)
        facets = [query_terms(s) for s in re.split(r"(?:^|\s)\d+[.)]\s*|\n+", query) if s.strip()]
        if len(facets) > 1:
            leading = []
            for facet in facets:
                def facet_score(p):
                    return sum((6 if t in re.sub(r"\s+", "", p["location"]).casefold() else 0) +
                               (2 if t in re.sub(r"\s+", "", p["text"]).casefold() else 0) for t in facet)
                if candidates:
                    best = max(candidates, key=facet_score)
                    if facet_score(best) > 0 and best not in leading:
                        leading.append(best)
            candidates = leading + [p for p in candidates if p not in leading]
    else:
        # 요약/범위 읽기는 한 절의 뒷부분보다 다른 절의 첫 문단을 먼저 제공한다.
        first, rest, seen = [], [], set()
        for item in candidates:
            (rest if item["location"] in seen else first).append(item)
            seen.add(item["location"])
        candidates = first + rest
    selected, used = [], 0
    # 문단 자체가 예산보다 크더라도 반드시 일부를 돌려주고 범위를 표시한다.
    for index, item in enumerate(candidates[offset:]):
        cost = min(len(item["location"]), 160) + 8
        room = max_chars - used - cost
        if room <= 0:
            break
        start = char_offset if index == 0 else 0
        remainder = item["text"][start:]
        excerpt = remainder[:room]
        if len(excerpt) < len(remainder):
            boundary = max(excerpt.rfind("\n"), excerpt.rfind(". ") + 1)
            if boundary > 0:
                excerpt = excerpt[:boundary]
            else:
                # 93.0%가 '9'처럼 잘려 새로운 수치가 되지 않게 한다.
                excerpt = re.sub(r"\d[\d,.]*$", "", excerpt)
            if not excerpt:
                break
        selected.append({**item, "text": excerpt, "start_char": start, "is_truncated": len(excerpt) < len(remainder)})
        used += cost + len(excerpt)
        if selected[-1]["is_truncated"]:
            break
    more = offset + len(selected) < len(candidates) or any(p["is_truncated"] for p in selected)
    partial = selected and selected[-1]["is_truncated"]
    return selected, {"total_passages": len(all_items), "matched_passages": len(candidates),
                      "returned_passages": len(selected),
                      "next_offset": offset + len(selected) - int(bool(partial)) if more else None,
                      "next_char_offset": selected[-1]["start_char"] + len(selected[-1]["text"]) if partial else 0,
                      "has_more": more, "scope": "selected_passages", "document_complete": False}
