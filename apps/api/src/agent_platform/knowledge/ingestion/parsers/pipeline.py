# -*- coding: utf-8 -*-
"""문서 파서 진입점: 파일 하나 → 구조화된 결과 (RAG 수집·프롬프트용 문서 공용).

    from agent_platform.knowledge.ingestion.parsers.pipeline import DocumentParser

    parser = DocumentParser()                              # 모델은 처음 쓸 때 한 번만 올린다
    r = parser.parse("원본.pdf")                            # 경로 (= document_version.storage_path)
    r = parser.parse(data, file_name="원본.hwpx")           # 업로드 바이트 (프롬프트용 첨부)

    r.status      # ok / rejected / failed      r.db_status  # ready / failed (document_version.status)
    r.reason      # 거절·실패 이유
    r.markdown    # 프롬프트에 바로 넣을 본문 (표는 마크다운 표)
    r.elements    # RAG 청킹용 항목 목록 (종류·글·쪽·위치·제목 경로·표 칸·표 만든 방법)
    r.log         # 단계별 처리 기록 (감사용)
    r.save_beside("원본.pdf", document_version_id=123)    # 원본 옆 parsed/123.json

형식별 경로 (router 판별 — 확장자를 믿지 않는다)
  HWPX            → 우리 추출기 (readers.hwpx) — 원본 구조 그대로
  HWP (바이너리)  → syhwp (readers.hwp) — 암호·배포용은 거절
  PDF · 이미지     → Docling(heron 배치, TableFormer accurate, RapidOCR) + 우리 후처리 (최종본 순서, 아래)
  DOCX·XLSX·PPTX·HTML·EML·CSV·MD → Docling
  그 밖(옛 Office, HWPML, 암호 PDF, 알 수 없음) → 거절 + 이유

PDF·이미지 후처리 순서 (2026-11-19 최종본 = 실험 PY15 설정과 같다)
  1) 글자 충실도(낱말): 둥근 따옴표·원문자·글꼴 지문 (text_fidelity) — 표 칸 글자가 낱말에서 오므로 먼저
  2) 스캔 쪽 다시 읽기 (ocr_reread) → 그림 속 글자 꺼내기 (picture_text) → 쪽 안 병합 칸 되살리기 (table_spans)
  3) 선 격자 표 (line_tables — 한컴 PDF 가 아니면 가상 테두리·늘려 만들기 격자도) → 규칙선 표 (rule_tables)
  4) 한컴 칸 클립 표 혼합 (clip_tables + kordoc_tables, 칸 클립 우선)
  5) 쪽 넘김 표 합치기 (table_merge)
  6) 글자 충실도(문서): 글 항목·표 칸의 따옴표·원문자

원칙: 예외를 밖으로 던지지 않는다. 실패도 status=failed + 이유로 돌려준다. 인터넷에서 아무것도 받지 않는다(폐쇄망).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

PARSER_VERSION = "parsers 0.2.0 (최종본 2026-11-19)"
SCHEMA = "parse_result/1"
MODELS_ENV = "PARSER_MODELS_DIR"   # 모델 폴더 (heron 배치, TableFormer, RapidOcr) — 용량이 커서 git 에 넣지 않는다

os.environ.setdefault("HF_HUB_OFFLINE", "1")          # 폐쇄망: 실행 중 모델을 내려받지 않는다
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


# ------------------------------------------------------------------ 결과
@dataclass
class ParseResult:
    status: str                                  # ok / rejected / failed
    reason: str = ""
    file_name: str = ""
    route: dict = field(default_factory=dict)    # format, group, handler, evidence
    pages: int | None = None
    elements: list = field(default_factory=list)
    markdown: str = ""
    log: dict = field(default_factory=dict)
    seconds: float = 0.0
    document: object = None                      # DoclingDocument (있으면) — JSON 에는 docling_document 로 들어간다

    @property
    def db_status(self) -> str:
        """document_version.status 값 (processing → ready / failed)."""
        return "ready" if self.status == "ok" else "failed"

    def to_dict(self, document_version_id=None, include_docling: bool = True) -> dict:
        d = {"schema": SCHEMA, "parser_version": PARSER_VERSION, "document_version_id": document_version_id,
             "file_name": self.file_name, "status": self.status, "db_status": self.db_status, "reason": self.reason,
             "route": self.route, "pages": self.pages, "seconds": self.seconds,
             "elements": self.elements, "markdown": self.markdown, "log": self.log}
        if include_docling and self.document is not None:
            d["docling_document"] = self.document.export_to_dict()
        return d

    def save_beside(self, original_path, document_version_id, include_docling: bool = True) -> Path:
        """원본 옆 parsed/{document_version_id}.json 으로 저장하고 그 경로를 돌려준다 (파서 입출력 계약)."""
        out = Path(original_path).parent / "parsed" / f"{document_version_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.to_dict(document_version_id, include_docling), ensure_ascii=False), encoding="utf-8")
        return out


# ------------------------------------------------------------------ 파서
class DocumentParser:
    def __init__(self, models_dir: str | Path | None = None, timeout: float = 600.0):
        """models_dir: 모델 폴더. 없으면 환경변수 PARSER_MODELS_DIR. 둘 다 없으면 PDF·이미지는 failed(이유: 모델 경로 없음),
        HWPX·HWP·DOCX 등 모델이 필요 없는 형식은 그대로 된다."""
        m = models_dir or os.environ.get(MODELS_ENV)
        self.models = Path(m) if m else None
        self.timeout = timeout
        self._converter = None
        self._ocr_engine = None

    # ---- 바깥 입구 ---------------------------------------------------------
    def parse(self, source, file_name: str | None = None) -> ParseResult:
        """source: 파일 경로 또는 바이트. 바이트면 file_name(확장자 참고용)을 준다. 예외를 던지지 않는다."""
        t0 = time.perf_counter()
        tmp = None
        try:
            if isinstance(source, (bytes, bytearray)):
                suffix = Path(file_name or "upload").suffix
                fd, tmp = tempfile.mkstemp(suffix=suffix)
                with os.fdopen(fd, "wb") as f:
                    f.write(source)
                path = Path(tmp)
                name = file_name or path.name
            else:
                path = Path(source)
                name = file_name or path.name
            res = self._parse_path(path, name)
        except Exception as e:                                  # 마지막 안전망
            res = ParseResult("failed", f"{type(e).__name__}: {str(e)[:300]}", file_name=file_name or "")
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        res.seconds = round(time.perf_counter() - t0, 2)
        return res

    # ---- 형식별 경로 --------------------------------------------------------
    def _parse_path(self, path: Path, name: str) -> ParseResult:
        from agent_platform.knowledge.ingestion.router import Handler, route

        r = route(path)
        info = {"format": r.format, "group": r.group.value, "handler": r.handler.value, "evidence": r.evidence}
        if r.handler == Handler.REJECT:
            return ParseResult("rejected", r.reject_reason, name, info)
        if r.handler == Handler.HWPML_EXTRACT:
            return ParseResult("rejected", "HWPML(XML) 리더 없음", name, info)
        if r.handler in (Handler.HWPX_EXTRACT, Handler.HWP_CONVERT):
            return self._parse_hangul(path, name, info, r.handler == Handler.HWPX_EXTRACT)
        return self._parse_docling(path, name, info, r)

    def _parse_hangul(self, path: Path, name: str, info: dict, hwpx: bool) -> ParseResult:
        from agent_platform.knowledge.ingestion.parsers.hangul import builder, headings
        from agent_platform.knowledge.ingestion.parsers.hangul.hwp import read_hwp
        from agent_platform.knowledge.ingestion.parsers.hangul.hwpx import read_hwpx

        data = path.read_bytes()
        r = read_hwpx(data) if hwpx else read_hwp(data)
        if r["status"] != "ok":
            why = r.get("reason") or {"encrypted": "암호가 걸린 문서", "distribution": "배포용 문서(읽기 제한)"}.get(r["status"], r["status"])
            return ParseResult("rejected" if r["status"] in ("encrypted", "distribution") else "failed", why, name, info)
        doc = builder.build(headings.assign(r["blocks"]), Path(name).stem)
        src = "원본(HWPX)" if hwpx else "원본(HWP·syhwp)"
        return self._result(doc, name, info, {}, {"reader": src}, default_table_source=src)

    def _parse_docling(self, path: Path, name: str, info: dict, r) -> ParseResult:
        from docling.datamodel.base_models import DocumentStream

        data = path.read_bytes()
        if r.encoding and r.encoding.lower().replace("-", "") not in ("utf8", "ascii"):
            data = data.decode(r.encoding, errors="replace").encode("utf-8")     # Docling 은 모르면 cp1252 로 추측한다
        if r.format in ("pdf", "image") and (self.models is None or not self.models.is_dir()):
            return ParseResult("failed", f"모델 폴더 없음 — DocumentParser(models_dir=...) 나 환경변수 {MODELS_ENV} 로 지정", name, info)
        stream = DocumentStream(name=f"{Path(name).stem}{r.docling_ext or path.suffix}", stream=__import__("io").BytesIO(data))
        conv = self._docling().convert(stream, raises_on_error=False)
        status = str(getattr(conv.status, "value", conv.status))
        if status not in ("success", "partial_success") or conv.document is None:
            errs = "; ".join(str(e.error_message)[:150] for e in (conv.errors or []))
            return ParseResult("failed", f"Docling 변환 실패({status}) {errs}".strip(), name, info)
        doc = conv.document
        log = {"docling": status}
        sources: dict[int, dict] = {}
        if r.format in ("pdf", "image"):
            log.update(self._postprocess(doc, path, data, r.format, sources))
        default = "모델(TableFormer)" if r.format in ("pdf", "image") else f"Docling({r.format})"
        return self._result(doc, name, info, sources, log, default_table_source=default)

    # ---- PDF·이미지 후처리 (최종본 순서) ------------------------------------
    def _postprocess(self, doc, path: Path, raw: bytes, fmt: str, sources: dict) -> dict:
        from agent_platform.knowledge.ingestion.parsers.pdf import ocr_reread
        from agent_platform.knowledge.ingestion.parsers.pdf.clip_tables import clip_tables_for_pdf, is_hancom_pdf
        from agent_platform.knowledge.ingestion.parsers.pdf.image_lines import read_lines
        from agent_platform.knowledge.ingestion.parsers.pdf.kordoc_tables import apply_kordoc_tables
        from agent_platform.knowledge.ingestion.parsers.pdf.line_tables import apply_line_tables
        from agent_platform.knowledge.ingestion.parsers.pdf.pdf_lines import read_pdf_words
        from agent_platform.knowledge.ingestion.parsers.pdf.picture_text import lift_picture_text
        from agent_platform.knowledge.ingestion.parsers.pdf.rule_tables import apply_rule_tables
        from agent_platform.knowledge.ingestion.parsers.pdf.table_merge import merge_split_tables
        from agent_platform.knowledge.ingestion.parsers.pdf.table_spans import fix_table_spans
        from agent_platform.knowledge.ingestion.parsers.pdf.text_fidelity import FidelityLog, fix_document, fix_words, read_quote_chars

        def tag(obj, **kw):
            """표 객체에 '만든 방법'을 붙인다 (각 단계 log.items = 그 단계가 바꾸거나 넣은 표 객체, log 기록과 같은 순서)."""
            sources[id(obj)] = {**sources.get(id(obj), {}), **kw}

        log: dict = {}
        lines = words = quotes = tflog = None
        if fmt == "pdf":
            lines = read_lines(raw)
            words = read_pdf_words(raw)
            quotes = read_quote_chars(raw)
            tflog = FidelityLog()
            words = fix_words(words, quotes, tflog)
        # 스캔 쪽(이미지 파일, 글자층 없는 PDF 쪽) 다시 읽기
        imgs = {}
        if fmt == "image":
            imgs = ocr_reread.page_images_from_file(raw, doc)
        elif words is not None:
            scan = {p for p in doc.pages if not words.get(p)}
            if scan:
                imgs = ocr_reread.page_images_from_pdf(raw, scan)
        if imgs:
            log["ocr_reread"] = ocr_reread.reread_document(doc, self._ocr(), imgs).pages
        log["picture_text"] = lift_picture_text(doc, words if words is not None else {}).pictures
        log["table_spans"] = fix_table_spans(doc, lines, words).tables
        if fmt == "pdf" and lines and words:
            relaxed = True
            try:
                from pypdf import PdfReader
                meta = PdfReader(str(path)).metadata or {}
                relaxed = not is_hancom_pdf(" | ".join(str(meta.get(k) or "") for k in ("/Producer", "/Creator")))
            except Exception:
                pass
            log["hancom_pdf"] = not relaxed
            llog = apply_line_tables(doc, lines, words, relaxed)
            for obj, x in zip(llog.items, llog.replaced):
                tag(obj, source="선 격자" + (" · 가상 테두리" if x.get("virtual_edges") else "") + (" · 늘려 만들기" if x.get("greedy") else ""))
            rtlog = apply_rule_tables(doc, lines, words)
            for obj in rtlog.items:
                tag(obj, source="규칙선")
            log["line_tables"] = {"replaced": llog.replaced, "skipped": llog.skipped}
            log["rule_tables"] = {"added": rtlog.added, "skipped": rtlog.skipped}
            # 칸 클립 혼합 — 가상 테두리·늘려 만들기로 만든 선 표는 보호하지 않는다(칸 클립이 원본 칸 그대로)
            prot = {x.get("ref") for x in [y for y in llog.replaced if not y.get("virtual_edges") and not y.get("greedy")]
                    + rtlog.added} - {None}
            keep = {x.get("ref") for x in rtlog.added} - {None}
            clog = apply_kordoc_tables(doc, clip_tables_for_pdf(path, words), words, prot, keep, trusted=True)
            for obj, x in zip(clog.items, clog.replaced):
                tag(obj, source="칸 클립", extra_pages=x.get("absorbed_pages") or [])
            log["clip_tables"] = {"replaced": clog.replaced, "kept": clog.kept}
        mlog = merge_split_tables(doc, lines)
        log["table_merge"] = {"merged": mlog.merged, "refused": mlog.refused}
        if tflog is not None:
            fix_document(doc, quotes, tflog)
            log["text_fidelity"] = {"words": tflog.words, "items": tflog.items, "cells": tflog.cells,
                                    "circled": tflog.circled, "skipped": len(tflog.skipped)}
        return log

    # ---- 결과 만들기 ---------------------------------------------------------
    def _result(self, doc, name: str, info: dict, sources: dict, log: dict, default_table_source: str) -> ParseResult:
        return ParseResult("ok", "", name, info, pages=len(doc.pages) or None,
                           elements=_elements(doc, sources, default_table_source), markdown=doc.export_to_markdown(),
                           log=log, document=doc)

    # ---- 무거운 것은 처음 쓸 때 한 번만 -----------------------------------------
    def _docling(self):
        if self._converter is None:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import (HeadingHierarchyOptions, OcrMode, PdfPipelineOptions,
                                                            RapidOcrOptions, TableFormerMode)
            from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

            ocr = RapidOcrOptions(lang=["korean"])
            ocr.mode = OcrMode.DEFAULT
            o = PdfPipelineOptions(artifacts_path=str(self.models) if self.models else None, do_ocr=True, ocr_options=ocr,
                                   do_table_structure=True, document_timeout=self.timeout)
            o.table_structure_options.mode = TableFormerMode.ACCURATE
            o.heading_hierarchy_options = HeadingHierarchyOptions(enabled=True)
            self._converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=o),
                                                                InputFormat.IMAGE: ImageFormatOption(pipeline_options=o)})
        return self._converter

    def _ocr(self):
        if self._ocr_engine is None:
            from agent_platform.knowledge.ingestion.parsers.pdf import ocr_reread
            self._ocr_engine = ocr_reread.make_engine(self.models / "RapidOcr")
        return self._ocr_engine


# ------------------------------------------------------------------ 항목 목록 (청킹용)
def _elements(doc, sources: dict, default_table_source: str) -> list[dict]:
    """DoclingDocument → [{type, text, page, pages, bbox(l,t,r,b 왼쪽 위 원점), heading_path, (표면) table, (목록 항목이면) marker}] 읽는 순서대로.
    청킹용 필드 2개: marker = 목록 항목의 번호("1.", "가." 등, PDF에서 본문과 따로 나옴), 표 칸의 h = Docling이 머리행으로 본 칸."""
    from docling_core.types.doc import ListItem, PictureItem, SectionHeaderItem, TableItem, TextItem, TitleItem

    out, path = [], []                             # path: [(단계, 제목)]
    for it, _ in doc.iterate_items():
        prov = list(getattr(it, "prov", None) or [])
        pages = sorted({p.page_no for p in prov})
        bbox = None
        if prov and prov[0].page_no in doc.pages:
            b = prov[0].bbox.to_top_left_origin(doc.pages[prov[0].page_no].size.height)
            bbox = [round(b.l, 1), round(b.t, 1), round(b.r, 1), round(b.b, 1)]
        label = str(getattr(it.label, "value", it.label))
        if isinstance(it, (TitleItem, SectionHeaderItem)):
            level = 0 if isinstance(it, TitleItem) else int(getattr(it, "level", 1) or 1)
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, it.text))
        el = {"type": label, "page": pages[0] if pages else None, "pages": pages, "bbox": bbox,
              "heading_path": [t for _, t in path]}
        if isinstance(it, TableItem):
            src = sources.get(id(it), {})
            more = src.get("extra_pages") or []
            el["pages"] = sorted(set(pages) | set(more))
            el["text"] = " ".join(c.text for c in it.data.table_cells if c.text)
            el["table"] = {"rows": it.data.num_rows, "cols": it.data.num_cols, "source": src.get("source", default_table_source),
                           "cells": [{"r": c.start_row_offset_idx, "c": c.start_col_offset_idx, "rs": c.row_span,
                                      "cs": c.col_span, "text": c.text, "h": bool(c.column_header)} for c in it.data.table_cells],
                           "markdown": it.export_to_markdown(doc)}
        elif isinstance(it, PictureItem):
            el["text"] = it.caption_text(doc) if hasattr(it, "caption_text") else ""
        elif isinstance(it, TextItem):
            el["text"] = it.text
            if isinstance(it, ListItem):
                el["marker"] = it.marker or ""
        else:
            continue
        out.append(el)
    return out
