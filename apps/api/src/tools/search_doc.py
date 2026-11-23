import asyncio
import json
from pathlib import Path

from contracts.harness import ToolSpec, ToolResult
from contracts.permission import ToolCall, Principal

from tools.read_doc import can_read_chunk, load_doc_chunks

PREVIEW_LENGTH = 200

SEARCH_DOC_SPEC = ToolSpec(
    name="search_doc",
    display_name="사내 문서 검색",
    effect="read",
    domain="문서",
    description=(
        "검색어와 관련된 사내 문서와 내규를 찾고 문단 앞부분 최대 200자를 preview로 반환합니다. "
        "환율·금리 같은 시장 수치나 고객·대출 데이터는 없고, 문서와 내규 문장만 찾습니다. "
        "is_truncated가 false이면 preview는 해당 문단 전체입니다. "
        "미리보기로 답할 수 있으면 제목과 위치를 근거로 바로 답하세요. "
        "긴 문단의 나머지 내용이 필요하면 chunk_id로 get_doc_chunk를 호출하세요."
    ),
    parameters={
        "type" : "object",
        "properties" : {
            "query" : {
                "type" : "string",
                "description" : "예시 : 서류 반려"
            },
        },
        "required" : ["query"],
    },
)

def search_chunks(chunks: list[dict], query:str, principal: Principal) -> list[dict]:
    needle = query.casefold()
    matches = []

    for chunk in chunks:
        if not can_read_chunk(chunk, principal):
            continue

        title_matches = needle in chunk["title"].casefold()
        text_matches = needle in chunk["text"].casefold()
        if not (title_matches or text_matches):
            continue

        matches.append({
             "doc_id": chunk["doc_id"],
            "chunk_id": chunk["chunk_id"],
            "title": chunk["title"],
            "location": chunk["location"],
            "preview": chunk["text"][:PREVIEW_LENGTH],
            "is_truncated": len(chunk["text"]) > PREVIEW_LENGTH,
        })

    return matches

class SearchDocsTool:
    spec = SEARCH_DOC_SPEC

    def __init__(self, doc_path : Path) -> None:
        self.doc_path = doc_path

    async def run(self, call: ToolCall) -> ToolResult:
        query = call.arguments.get("query")
        
        if not isinstance(query, str):
            return ToolResult(call.call_id, False, "검색어를 문자열로 입력해 주세요.")

        query = query.strip()

        if not query:
            return ToolResult(call.call_id, False, "검색어를 입력해 주세요.")

        try:
            chunks = await asyncio.to_thread(
                load_doc_chunks,
                self.doc_path,
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ToolResult(call.call_id, False, "문서 목록을 읽을 수 없습니다.")

        matches = search_chunks(chunks, query, call.principal)
        payload = {"query": query, "matches": matches}

        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)
