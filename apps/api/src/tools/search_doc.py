import asyncio
import json
from pathlib import Path

from contracts.harness import ToolSpec, ToolResult
from contracts.permission import Principal, ToolCall

from tools.read_doc import can_read_chunk, load_doc_chunks

SEARCH_DOC_SPEC = ToolSpec(
    name="search_doc",
    display_name="사내 문서 검색",
    description="검색어와 관련된 사내 문서와 내규를 찾습니다.",
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
        })

    return matches

class SearchDocumentsTool:
    spec = SEARCH_DOC_SPEC

    def __init__(self, doc_path : Path) -> None:
        self.document_path = doc_path

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
                self.document_path,
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ToolResult(call.call_id, False, "문서 목록을 읽을 수 없습니다.")

        matches = search_chunks(chunks, query, call.principal)
        payload = {"query": query, "matches": matches}

        return ToolResult(call.call_id, True, json.dumps(payload, ensure_ascii=False), data=payload)