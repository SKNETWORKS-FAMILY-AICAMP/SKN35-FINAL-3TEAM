# -*- coding: utf-8 -*-
"""글자 충실도 바로잡기: 글자층이 바꿔 읽은 글자를 원본 PDF 글자로 되돌린다.

왜 필요한가
  Docling 글자층(docling_parse)은 둥근 따옴표(‘ ’ “ ”)를 곧은 따옴표(' ")로 바꿔 읽는다. v2·v3 PDF 30개 중 24개에서
  둥근 따옴표 338자가 전부 바뀌었다(pdfium 으로 읽은 같은 PDF와 비교). 본문·표 칸 모두 같은 글자층에서 오므로
  원문 인용("…" 조문 인용, ’25.9 같은 연월)이 원본과 달라진다 — 규제 문서 답변 근거는 원문 그대로여야 한다.
  또 한컴 PDF는 원문자를 원(◯) + 숫자로 겹쳐 그려, 글자층에 "◯1"로 나온다(정답·원본 HWPX 는 "①").

하는 일
  1) 따옴표: 같은 PDF를 pdfium(이미 Docling 의존성)으로 읽어 따옴표 글자의 위치와 모양을 모은다. 낱말·글 항목·표 칸마다
     그 자리 안의 따옴표 글자를 읽는 순서(줄 위→아래, 줄 안 왼→오른)로 늘어놓고, 우리 글의 따옴표 자리 수와 같을 때만
     차례대로 원본 모양으로 바꾼다 (수가 다르면 손대지 않는다 — 엉뚱한 자리를 바꾸지 않게).
  2) 원문자: "◯" 바로 뒤 1~20 숫자를 원문자(①~⑳)로 합친다.
  3) 글자 코드 없는 한컴 글리프(U+F000 — 원문자 번호·[예]·글머리 기호): PDF 에 든 글꼴 윤곽의 지문을 kordoc 지문표(hancom_glyphs)와
     맞춰 되살린다. 지문이 정확히 같을 때만.
  하지 않는 것: 지문표에 없는 글리프의 모양 추정, 다른 글자 바꾸기.

좌표: 낱말·결과는 왼쪽 위 원점(우리 낱말 형식과 같음).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

QUOTES = "'\"‘’“”"          # 서로 바뀌어 읽히는 따옴표 묶음 — 곧은 것도 넣어야 차례가 맞는다
FIXABLE = QUOTES + chr(0xF000)  # 바로잡는 글자 자리: 따옴표 + 글자 코드 없는 한컴 글리프(U+F000)
BOX_TOL = 1.5               # 글자 가운데가 자리 상자 밖으로 이만큼까지는 안 (pt)
LINE_TOL = 0.6              # 같은 줄 — 글자 가운데 높이 차가 글자 높이의 이 배 안
CIRCLED = re.compile(r"◯\s?(1[0-9]|20|[1-9])(?!\d)")


@dataclass
class FidelityLog:
    words: int = 0           # 바꾼 낱말 수
    items: int = 0           # 바꾼 글 항목 수
    cells: int = 0           # 바꾼 표 칸 수
    circled: int = 0         # 합친 원문자 수
    skipped: list = field(default_factory=list)   # 수가 안 맞아 둔 자리 (쪽, 종류, 우리 수, 원본 수)


# ------------------------------------------------------------------ 글자 코드 없는 한컴 글리프 (U+F000) 되살리기
# 한컴 PDF 는 ToUnicode 를 못 만든 글리프(원문자 번호·[예]·글머리 기호 등)를 전부 U+F000 으로 적는다. 글자는 PDF 에 든 글꼴의
# 윤곽으로만 남는다. kordoc(symbol-glyphs.ts restoreUnmappedGlyphs · known-font-glyphs.ts, MIT)처럼 그 글리프 윤곽의 지문을
# 확인된 지문표(hancom_glyphs)와 맞춰 한컴 사설 코드 → 표준 글자로 옮긴다. 지문이 정확히 같을 때만 바꾼다 — 모양 추정은 하지 않는다
# (kordoc classifyOutline 의 삼각형·네모 추정은 옮기지 않았다).
UNMAPPED = chr(0xF000)


def _u16(b, o):
    return (b[o] << 8) | b[o + 1]


def _i16(b, o):
    v = _u16(b, o)
    return v - 0x10000 if v & 0x8000 else v


def _u32(b, o):
    return int.from_bytes(b[o:o + 4], "big")


def ttf_outline(b: bytes, gid: int):
    """TrueType 단순 글리프 윤곽 → ([[ [x, y, 곡선 위 점], … ], …], upm). 합성 글리프·CFF 는 None (kordoc readOutline 과 같다)."""
    try:
        n = _u16(b, 4)
        t = {b[12 + 16 * i:16 + 16 * i].decode("latin-1"): _u32(b, 12 + 16 * i + 8) for i in range(n)}
        head, loca, glyf = t.get("head"), t.get("loca"), t.get("glyf")
        if head is None or loca is None or glyf is None:
            return None
        long_loca = _i16(b, head + 50) == 1
        a = _u32(b, loca + 4 * gid) if long_loca else 2 * _u16(b, loca + 2 * gid)
        z = _u32(b, loca + 4 * gid + 4) if long_loca else 2 * _u16(b, loca + 2 * gid + 2)
        if z <= a:
            return None
        g = glyf + a
        nc = _i16(b, g)
        if nc <= 0 or nc > 8:
            return None
        ends = [_u16(b, g + 10 + 2 * i) for i in range(nc)]
        npt = ends[-1] + 1
        if npt > 400:
            return None
        o = g + 10 + 2 * nc
        o += 2 + _u16(b, o)
        flags = []
        while len(flags) < npt:
            f = b[o]; o += 1
            flags.append(f)
            if f & 8:
                rep = b[o]; o += 1
                flags += [f] * rep
        xs, ys, x, y = [], [], 0, 0
        for f in flags:
            if f & 2:
                d = b[o]; o += 1
                x += d if f & 16 else -d
            elif not f & 16:
                x += _i16(b, o); o += 2
            xs.append(x)
        for f in flags:
            if f & 4:
                d = b[o]; o += 1
                y += d if f & 32 else -d
            elif not f & 32:
                y += _i16(b, o); o += 2
            ys.append(y)
        cs, s = [], 0
        for e in ends:
            cs.append([[xs[k], ys[k], bool(flags[k] & 1)] for k in range(s, e + 1)])
            s = e + 1
        return cs, (_u16(b, head + 18) or 1000)
    except (IndexError, ValueError):
        return None


def known_pua(font_name: str, outline, width) -> int | None:
    """확인된 한컴 글꼴의 지문이면 그 글리프의 한컴 사설 코드 (kordoc knownFontPua)."""
    import hashlib
    import json
    from knowledge.ingestion.parsers.pdf.hancom_glyphs import FACES, KNOWN

    face = FACES.get(re.sub(r"^[A-Z]{6}\+", "", font_name or ""))
    if not face or outline is None:
        return None
    cs, upm = outline
    w = int(width) if float(width) == int(width) else float(width)     # JS JSON 숫자 모양 (1000.0 → 1000)
    key = hashlib.sha256(json.dumps([upm, w, cs], separators=(",", ":")).encode()).hexdigest()[:16]
    hit = KNOWN.get(key)
    return hit[0] if hit and hit[1] & face else None


def _enclosed(n: int) -> str:
    if n == 0:
        return "⓪"
    n -= 1
    if n < 20:
        return chr(0x2460 + n)
    if n < 35:
        return chr(0x3251 + n - 20)
    if n < 50:
        return chr(0x32B1 + n - 35)
    return f"({n + 1})"


def _tens(c):
    return c - 0xF0288 if 0xF0288 <= c <= 0xF0291 else (c - 0xF02B9 if 0xF02BA <= c <= 0xF02C2 else None)


def _units(c):
    return c - 0xF0292 if 0xF0292 <= c <= 0xF029B else (c - 0xF02C3 if 0xF02C3 <= c <= 0xF02CC else None)


def map_pua_code(c: int) -> str | None:
    """한컴 사설 코드 → 표준 글자 (kordoc shared/pua.ts numberGlyph · SUPPLEMENTARY_MAP)."""
    from knowledge.ingestion.parsers.pdf.hancom_glyphs import SUPPLEMENTARY

    t = _tens(c)
    if t is not None:
        return _enclosed(t)
    if 0xF02B0 <= c <= 0xF02B9:
        return _enclosed(c - 0xF02B0)
    u = _units(c)
    if u is not None:
        return str(u)
    return SUPPLEMENTARY.get(c)


def _unmapped_codes(font) -> set[int]:
    """Type0 글꼴의 ToUnicode 에서 U+F000 으로 적힌 글자 코드 (bfchar 와 한 값 bfrange)."""
    tu = font.get("/ToUnicode")
    if tu is None:
        return set()
    try:
        s = tu.get_object().get_data().decode("latin-1")
    except Exception:
        return set()
    out = {int(m[1], 16) for m in re.finditer(r"<([0-9A-Fa-f]{2,4})>\s*<[Ff]000>", s)}
    for m in re.finditer(r"<([0-9A-Fa-f]{2,4})>\s*<([0-9A-Fa-f]{2,4})>\s*<[Ff]000>", s):
        if m[1].lower() == m[2].lower():
            out.add(int(m[1], 16))
    return out


def _cid_widths(df) -> tuple[dict, float]:
    W, out, i = df.get("/W") or [], {}, 0
    try:
        while i < len(W):
            st, nx = int(W[i]), W[i + 1]
            if hasattr(nx, "__len__"):
                for k, w in enumerate(nx):
                    out[st + k] = float(w)
                i += 2
            else:
                for c in range(st, int(nx) + 1):
                    out[c] = float(W[i + 2])
                i += 3
    except (IndexError, ValueError, TypeError):
        pass
    return out, float(df.get("/DW", 1000))


def read_unmapped_glyphs(pdf_bytes: bytes) -> dict[int, dict[str, list[str]]]:
    """PDF → {쪽: {글꼴 이름: [U+F000 글리프마다 되살린 글(모르면 U+F000), 내용 스트림 차례]}}.
    두 자리 번호(십의 자리 조각 + 일의 자리 숫자를 겹쳐 찍음)는 앞 글리프에 번호, 뒤 글리프는 빈 글."""
    import io

    from pypdf import PdfReader
    from pypdf.generic import ContentStream

    reader = PdfReader(io.BytesIO(pdf_bytes))
    out: dict[int, dict[str, list[str]]] = {}
    font_cache: dict[int, tuple] = {}

    def font_info(fobj):
        key = id(fobj)
        if key not in font_cache:
            info = None
            try:
                f = fobj.get_object()
                if f.get("/Subtype") == "/Type0":
                    codes = _unmapped_codes(f)
                    if codes:
                        df = f["/DescendantFonts"][0].get_object()
                        ff = df["/FontDescriptor"].get_object().get("/FontFile2")
                        data = ff.get_object().get_data() if ff is not None else None
                        widths, dw = _cid_widths(df)
                        name = str(f.get("/BaseFont", ""))[1:] if str(f.get("/BaseFont", "")).startswith("/") else str(f.get("/BaseFont", ""))
                        info = (name, codes, data, widths, dw, {})
            except Exception:
                info = None
            font_cache[key] = info
        return font_cache[key]

    def walk(content, resources, queues, depth=0):
        if content is None or depth > 4:
            return
        try:
            ops = ContentStream(content, None).operations
        except Exception:
            return
        fonts = (resources or {}).get("/Font") or {}
        fonts = fonts.get_object() if hasattr(fonts, "get_object") else fonts
        cur = None
        for operands, op in ops:
            op = op.decode("latin-1") if isinstance(op, bytes) else op
            if op == "Tf" and operands:
                fo = fonts.get(operands[0]) if fonts else None
                cur = font_info(fo) if fo is not None else None
            elif op in ("Tj", "TJ", "'", '"') and cur is not None:
                strs = operands[0] if op == "TJ" else operands[-1:]
                name, codes, data, widths, dw, cache = cur
                for s in strs:
                    if isinstance(s, (int, float)) or not hasattr(s, "__len__"):
                        continue
                    raw = getattr(s, "original_bytes", None) or bytes(s)
                    for k in range(0, len(raw) - 1, 2):              # Identity-H 두 바이트 코드 (CID = 글리프 번호)
                        cid = (raw[k] << 8) | raw[k + 1]
                        if cid not in codes:
                            continue
                        if cid not in cache:
                            pua = known_pua(name, ttf_outline(data, cid), widths.get(cid, dw)) if data else None
                            cache[cid] = pua
                        queues.setdefault(name, []).append(cache[cid])
            elif op == "Do" and resources is not None:
                xo = resources.get("/XObject")
                xo = xo.get_object() if xo is not None else None
                obj = xo.get(operands[0]).get_object() if xo and operands[0] in xo else None
                if obj is not None and obj.get("/Subtype") == "/Form":
                    walk(obj, obj.get("/Resources") or resources, queues, depth + 1)

    for i, page in enumerate(reader.pages):
        queues: dict[str, list] = {}
        try:
            res = page.get("/Resources")
            walk(page.get_contents(), res.get_object() if res is not None else None, queues)
        except Exception:
            queues = {}
        texts = {}
        for name, q in queues.items():
            t = []
            k = 0
            while k < len(q):
                a = q[k]
                b = q[k + 1] if k + 1 < len(q) else None
                if a is not None and b is not None and _tens(a) is not None and _units(b) is not None:
                    t += [_enclosed(_tens(a) * 10 + _units(b)), ""]
                    k += 2
                    continue
                t.append((map_pua_code(a) if a is not None else None) or UNMAPPED)
                k += 1
            texts[name] = t
        if texts:
            out[i + 1] = texts
    return out


def read_quote_chars(pdf_bytes: bytes, glyphs: bool = True) -> dict[int, list[tuple[str, float, float, float]]]:
    """PDF → {쪽: [(바로잡을 글, 가운데 x, 가운데 y(왼쪽 위 원점), 글자 높이)]} (pdfium 글자 상자).
    따옴표는 원본 모양 그대로, U+F000 글리프는 되살린 글(glyphs — 모르면 U+F000 그대로)."""
    import ctypes

    import pypdfium2 as pdfium
    import pypdfium2.raw as R

    restored = read_unmapped_glyphs(pdf_bytes) if glyphs else {}
    out: dict[int, list] = {}
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        for i in range(len(doc)):
            page = doc[i]
            H = page.get_height()
            tp = page.get_textpage()
            n = tp.count_chars()
            text = tp.get_text_range(0, n)                  # 글자 번호와 같은 차례 (pdfium 이 만든 줄바꿈 글자 포함)
            chars, pua = [], []
            for k in range(min(n, len(text))):
                ch = text[k]
                if ch not in FIXABLE:
                    continue
                l, b, r, t = tp.get_charbox(k)
                rec = [ch, (l + r) / 2, H - (b + t) / 2, max(1.0, t - b)]
                if ch == UNMAPPED:
                    buf = ctypes.create_string_buffer(256)
                    R.FPDFText_GetFontInfo(tp.raw, k, buf, 256, None)
                    pua.append((buf.value.decode("latin-1", "ignore"), rec))
                chars.append(rec)
            # 글꼴마다 pdfium 의 U+F000 차례와 내용 스트림 글리프 차례를 짝짓는다 — 수가 다르면 그 글꼴은 손대지 않는다 (kordoc 과 같다)
            pq = restored.get(i + 1, {})
            by_font: dict[str, list] = {}
            for name, rec in pua:
                by_font.setdefault(name, []).append(rec)
            for name, recs in by_font.items():
                q = pq.get(name)
                if q is not None and len(q) == len(recs):
                    for rec, tgt in zip(recs, q):
                        rec[0] = tgt
            out[i + 1] = [tuple(c) for c in chars]
    finally:
        doc.close()
    return out


def _inside(q, box) -> bool:
    l, t, r, b = box
    return l - BOX_TOL <= q[1] <= r + BOX_TOL and t - BOX_TOL <= q[2] <= b + BOX_TOL


def _reading_order(qs):
    """따옴표 글자들을 읽는 순서로 — 줄(가운데 높이)로 묶고 줄 안은 왼→오른."""
    lines: list[list] = []
    for q in sorted(qs, key=lambda q: q[2]):
        if lines and abs(lines[-1][-1][2] - q[2]) <= LINE_TOL * max(q[3], lines[-1][-1][3]):
            lines[-1].append(q)
        else:
            lines.append([q])
    return [q for ln in lines for q in sorted(ln, key=lambda q: q[1])]


def restore_quotes(text: str, qs) -> str | None:
    """text 안 바로잡을 자리(따옴표·U+F000)를 qs(읽는 순서)의 글로 차례대로 바꾼다. 수가 다르면 None.
    되살린 글은 여러 글자("[예]")이거나 빈 글(두 자리 번호의 뒤 조각)일 수 있다."""
    pos = [i for i, ch in enumerate(text) if ch in FIXABLE]
    if not pos or len(pos) != len(qs):
        return None
    out = list(text)
    for i, q in zip(pos, qs):
        out[i] = q[0]
    return "".join(out)


def compose_circled(text: str) -> tuple[str, int]:
    """"◯1" → "①" (1~20)."""
    n = 0

    def rep(m):
        nonlocal n
        n += 1
        return chr(0x2460 + int(m[1]) - 1)
    return CIRCLED.sub(rep, text), n


def fix_words(words: dict[int, list], quotes: dict[int, list], log: FidelityLog | None = None) -> dict[int, list]:
    """우리 낱말 [(글자, l, t, r, b)] 의 따옴표·원문자를 바로잡은 새 목록."""
    log = log or FidelityLog()
    out = {}
    for pg, ws in words.items():
        qs = quotes.get(pg) or []
        new = []
        for w in ws:
            text = w[0]
            if any(ch in FIXABLE for ch in text) and qs:
                fixed = restore_quotes(text, sorted((q for q in qs if _inside(q, w[1:5])), key=lambda q: q[1]))
                if fixed is not None and fixed != text:
                    text = fixed
                    log.words += 1
            text, k = compose_circled(text)
            log.circled += k
            new.append((text,) + tuple(w[1:]))
        out[pg] = new
    return out


def _fix_text(text, qs_in_box, log, kind, page):
    """한 자리(글 항목·표 칸)의 글 바로잡기 → (새 글, 바뀌었나)."""
    new = text
    if any(ch in FIXABLE for ch in text) and qs_in_box:
        fixed = restore_quotes(text, _reading_order(qs_in_box))
        if fixed is None:
            log.skipped.append((page, kind, sum(ch in FIXABLE for ch in text), len(qs_in_box)))
        else:
            new = fixed
    new, k = compose_circled(new)
    log.circled += k
    return new, new != text


def fix_document(doc, quotes: dict[int, list], log: FidelityLog | None = None) -> FidelityLog:
    """DoclingDocument 의 글 항목·표 칸 글자를 바로잡는다 (제자리 수정)."""
    from docling_core.types.doc import TableItem, TextItem

    log = log or FidelityLog()
    for it, _ in doc.iterate_items():
        if isinstance(it, TextItem) and it.prov:
            p = it.prov[0]
            H = doc.pages[p.page_no].size.height
            b = p.bbox.to_top_left_origin(H)
            qs = [q for q in quotes.get(p.page_no) or [] if _inside(q, (b.l, b.t, b.r, b.b))]
            new, changed = _fix_text(it.text, qs, log, "글", p.page_no)
            if changed:
                it.text = new
                if getattr(it, "orig", None) is not None:
                    it.orig = new
                log.items += 1
        elif isinstance(it, TableItem) and it.prov:
            pg = it.prov[0].page_no
            H = doc.pages[pg].size.height
            for c in it.data.table_cells:
                qs = []
                if c.bbox is not None:
                    cb = c.bbox.to_top_left_origin(H)
                    qs = [q for q in quotes.get(pg) or [] if _inside(q, (cb.l, cb.t, cb.r, cb.b))]
                new, changed = _fix_text(c.text, qs, log, "칸", pg)
                if changed:
                    c.text = new
                    log.cells += 1
    return log
