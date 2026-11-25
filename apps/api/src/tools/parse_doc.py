"""업로드된 문서 읽기 툴: 에이전트가 대화 중에 받은 문서(HWPX·HWP·PDF·이미지·DOCX 등)를 파싱해 내용을 돌려준다.

- RAG 적재는 이 툴을 거치지 않는다. 적재 쪽은 knowledge.ingestion.parsers.DocParser 를 직접 부른다.
- 인자는 업로드 API가 발급한 file_id다. 플랫폼은 소유자를 검사하며 서버 경로는 받지 않는다.
- 한 번 파싱한 결과는 원본 옆 parsed/{file_id}.json (파서 입출력 계약) 으로 남기고, 다음 호출부터는 그걸 읽는다.
- PDF·이미지는 표 인식 모델 때문에 오래 걸릴 수 있어서, 이 툴은 하네스 기본 제한시간 대신 자기 timeout 을 쓴다.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from threading import Lock

from contracts.harness import ToolResult, ToolSpec
from contracts.permission import ToolCall
from services.uploads import UploadStore
from knowledge.ingestion.parsers import DocParser, ParseResult
from knowledge.ingestion.parsers.pipeline import PARSER_VERSION
from tools.document_passages import select_passages

MAX_CHARS = 20_000  # LLM 에게 돌려줄 본문 글자 수 상한. 넘으면 자르고 잘렸다고 알린다
DEFAULT_TIMEOUT = 600.0  # 이 툴만의 실행 제한시간(초). 러너가 tool.timeout 을 보고 기본값 대신 쓴다

PARSE_DOC_SPEC = ToolSpec(
    name="parse_doc",
    display_name="업로드 문서 읽기",
    effect="read",
    domain="문서",
    description=(
        "사용자가 첨부한 파일 목록의 file_id로 문서를 읽어 본문을 마크다운으로 돌려줍니다. 표는 마크다운 표로 들어 있습니다. "
        "query로 질문에 관련된 문단을 찾고 section으로 절 제목을 지정할 수 있습니다. "
        "쪽 정보가 없는 HWPX는 pages 대신 query·section을 사용하세요. 부분 결과만으로 문서 전체에 없다고 단정하지 마세요."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_id": {"type": "string", "description": "첨부 목록의 file_id 전체를 복사하세요. 끝의 확장자(.hwpx 등)도 포함해야 합니다. 이름이나 경로를 추측하지 마세요."},
            "query": {"type": "string", "maxLength": 2000, "description": "이 첨부 문서 안에서 찾을 질문/검색어. 사내 문서 검색과 별개입니다."},
            "section": {"type": "string", "maxLength": 200, "description": "읽을 절 제목의 일부"},
            "offset": {"type": "integer", "minimum": 0, "description": "결과의 next_offset으로 이어 읽기"},
            "char_offset": {"type": "integer", "minimum": 0, "maximum": 700, "description": "next_char_offset으로 잘린 문단의 나머지 읽기. query·section은 그대로 유지"},
            "max_chars": {"type": "integer", "minimum": 256, "maximum": MAX_CHARS, "description": "반환할 본문 분량. 작은 컨텍스트에서는 1800 권장"},
            "pages": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 2,
                "maxItems": 2,
                "description": "읽을 쪽 범위 [시작, 끝] (1부터, 끝 포함). 없으면 문서 전체",
            },
        },
        "required": ["file_id"],
    },
)


def resolve_upload(upload_dir: Path, file_id: object) -> Path | None:
    """file_id 를 업로드 폴더 바로 아래 파일 경로로 바꾼다. 폴더 밖·하위 폴더·숨김 파일·없는 파일이면 None."""
    if not isinstance(file_id, str):
        return None
    name = file_id.strip()
    # 경로 구분자나 '..' 이 들어 있으면 이름이 아니라 경로다 → 거절
    if not name or name.startswith(".") or Path(name).name != name or "\\" in name or "/" in name or ":" in name:
        return None
    path = (upload_dir / name).resolve()
    if path.parent != upload_dir.resolve() or not path.is_file():
        return None
    return path


def parse_pages(value: object) -> tuple[int, int] | None | str:
    """pages 인자 → (시작, 끝). 없으면 None, 형식이 틀리면 에러 문구(str)."""
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 2
            or not all(type(v) is int for v in value) or value[0] < 1 or value[1] < value[0]):
        return "pages 는 [시작쪽, 끝쪽] 형태의 1 이상 정수 두 개여야 합니다."
    return value[0], value[1]


def load_cache(cache: Path, original: Path) -> dict | None:
    """저장해 둔 파싱 결과를 쓸 수 있으면 dict, 아니면 None.
    원본이 그 뒤에 바뀌었거나, 파서 버전이 다르거나, 실패 결과였으면 다시 파싱한다."""
    try:
        if cache.stat().st_mtime < original.stat().st_mtime:
            return None
        d = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict) or d.get("parser_version") != PARSER_VERSION or d.get("status") != "ok":
        return None
    if not isinstance(d.get("markdown"), str) or not isinstance(d.get("elements"), list):
        return None
    return d


def element_text(el: dict) -> str:
    """요소 하나를 마크다운 한 덩어리로. 쪽 범위로 자를 때 전체 markdown 대신 이걸 이어 붙인다."""
    if "table" in el:
        return el["table"].get("markdown", "")
    text = el.get("text") or ""
    if el.get("type") == "title":
        return f"# {text}"
    if el.get("type") == "section_header":
        return f"## {text}"
    return text


def build_payload(file_id: str, d: dict, page_range: tuple[int, int] | None, cached: bool, *,
                  query: str = "", section: str = "", offset: int = 0, char_offset: int = 0,
                  max_chars: int | None = None) -> dict:
    """파싱 결과 dict → 툴 결과. 쪽 범위가 있으면 그 쪽에 걸친 요소만 모아 본문을 만든다."""
    elements = d.get("elements") or []
    if page_range:
        lo, hi = page_range
        # 여러 쪽에 걸친 표(이어진 표)도 범위와 한 쪽이라도 겹치면 넣는다
        elements = [e for e in elements if any(lo <= p <= hi for p in (e.get("pages") or [e.get("page")]) if p)]
        text = "\n\n".join(t for t in map(element_text, elements) if t)
    else:
        text = d.get("markdown") or ""
    limit = max_chars if max_chars is not None else MAX_CHARS
    evidence = []
    if query or section or offset or char_offset or max_chars is not None:
        evidence, read_status = select_passages({**d, "elements": elements, "markdown": text}, query=query, section=section,
                                                 offset=offset, char_offset=char_offset, max_chars=limit)
        text = "\n\n".join(f"【{p['location'][-160:]}】\n{p['text']}" for p in evidence)
        truncated = read_status["has_more"]
    else:
        truncated = len(text) > limit
        all_pages = page_range is None or (page_range[0] == 1 and page_range[1] >= (d.get("pages") or 0))
        read_status = {"scope": "pages" if page_range else "document", "document_complete": not truncated and all_pages,
                       "has_more": truncated, "next_offset": None}
    return {
        "file_id": file_id,
        "pages": d.get("pages"),  # 문서 전체 쪽수 (HWPX 처럼 쪽 개념이 없으면 null)
        "page_range": list(page_range) if page_range else None,
        "tables": sum(1 for e in elements if "table" in e),
        "truncated": truncated,
        "text": text[:limit],
        "cached": cached,
        "read_status": read_status,
        "passages": [{"passage_id": p["passage_id"], "location": p["location"]} for p in evidence],
        "_evidence": evidence,
    }


class ParseDocTool:
    spec = PARSE_DOC_SPEC

    def __init__(self, upload_dir: Path, parser: DocParser | None = None, timeout: float | None = DEFAULT_TIMEOUT,
                 uploads: UploadStore | None = None) -> None:
        self.upload_dir = Path(upload_dir)
        # 파서는 툴과 함께 한 번만 만든다 → 모델(배치·표 인식·OCR)을 첫 PDF 때 한 번만 읽고 계속 쓴다
        self.parser = parser or DocParser()
        self.timeout = timeout
        self.uploads = uploads
        # Docling 변환기를 여러 스레드가 동시에 쓰지 않도록 한 번에 한 문서씩 파싱한다
        self._lock = Lock()

    def _parse_and_save(self, path: Path, file_id: str) -> ParseResult:
        """(스레드에서 실행) 파싱하고, 성공하면 원본 옆 parsed/{file_id}.json 으로 남긴다."""
        # asyncio 대기가 취소돼도 실제 파싱 스레드가 끝날 때까지 변환기 잠금을 유지한다.
        with self._lock:
            result = self.parser.parse(path)
            if result.status == "ok":
                try:
                    result.save_beside(path, doc_version_id=file_id)
                except OSError:
                    pass  # 저장 실패는 캐시만 못 쓰는 것이다. 이번 결과는 그대로 돌려준다
            return result

    async def run(self, call: ToolCall) -> ToolResult:
        attachment = None
        if self.uploads is not None:
            file_id = call.arguments.get("file_id")
            if not isinstance(file_id, str):
                return ToolResult(call.call_id, False, "열람 가능한 첨부 파일을 찾을 수 없습니다.")
            attachment = await asyncio.to_thread(self.uploads.get_owned, file_id, call.principal.user_id)
            if attachment is None:
                return ToolResult(call.call_id, False, "열람 가능한 첨부 파일을 찾을 수 없습니다. 첨부 목록의 file_id 전체(확장자 포함)를 그대로 사용해 주세요.")
        path = resolve_upload(self.upload_dir, call.arguments.get("file_id"))
        if path is None:
            return ToolResult(call.call_id, False, "업로드된 파일을 찾을 수 없습니다. 파일 이름을 확인해 주세요.")
        page_range = parse_pages(call.arguments.get("pages"))
        if isinstance(page_range, str):
            return ToolResult(call.call_id, False, page_range)
        query, section = call.arguments.get("query", ""), call.arguments.get("section", "")
        offset, limit = call.arguments.get("offset", 0), call.arguments.get("max_chars")
        char_offset = call.arguments.get("char_offset", 0)
        if (not isinstance(query, str) or len(query) > 2000 or not isinstance(section, str) or len(section) > 200
                or type(offset) is not int or offset < 0
                or type(char_offset) is not int or not 0 <= char_offset <= 700
                or (limit is not None and (type(limit) is not int or not 256 <= limit <= MAX_CHARS))):
            return ToolResult(call.call_id, False, "문서 검색어·절 제목·읽기 범위를 확인해 주세요.")

        file_id = path.name
        cache = path.parent / "parsed" / f"{file_id}.json"
        d = await asyncio.to_thread(load_cache, cache, path)
        cached = d is not None
        if d is None:
            result = await asyncio.to_thread(self._parse_and_save, path, file_id)
            if result.status != "ok":
                # rejected(암호 문서·지원 안 하는 형식) / failed(읽다 실패) — 이유를 그대로 알려 준다
                return ToolResult(call.call_id, False, f"문서를 읽지 못했습니다: {result.reason}",
                                  data={"file_id": file_id, "status": result.status, "reason": result.reason})
            d = result.to_dict(include_docling=False)

        no_pages = page_range is not None and d.get("pages") is None
        if no_pages:
            page_range = None
        payload = await asyncio.to_thread(build_payload, file_id, d, page_range, cached,
                                          query=query.strip(), section=section.strip(),
                                          offset=offset, char_offset=char_offset, max_chars=limit)
        evidence = payload.pop("_evidence")
        if no_pages:
            payload["processing_status"] = {"page_filter_applied": False,
                                            "notice": "쪽 정보가 없어 문단 기준으로 읽었습니다. 문서 본문이 아닌 처리 상태입니다."}
        if attachment is not None:
            payload["filename"] = attachment["filename"]
        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False),
                          data={**payload, "status": d.get("status"), "elements": d.get("elements"), "evidence": evidence})
