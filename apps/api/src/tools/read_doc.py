import json
import asyncio
from pathlib import Path
from contracts.permission import Principal
from contracts.harness import ToolSpec, ToolResult
from contracts.permission import ToolCall, Principal

def load_doc_chunks(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)

def can_read_chunk(chunk: dict, principal: Principal) -> bool:
    user_grade = principal.attributes.get("security_grade")
    required_grade = chunk.get("security_grade")

    if type(user_grade) is not int or type(required_grade) is not int:
        return False

    if user_grade < required_grade:
        return False

    scope = chunk.get("scope")

    if scope == "company":
        return True

    if scope == "dept":
        user_dept = principal.attributes.get("dept_id")
        doc_dept = chunk.get("dept_id")

        return (
            type(user_dept) is int
            and type(doc_dept) is int
            and user_dept == doc_dept
        )

    return False

READ_DOC_SPEC = ToolSpec(
    name="get_doc_chunk",
    display_name="문서 원문 열람",
    effect="read",
    domain="문서",
    description="검색 결과의 chunk_id로 열람 가능한 문단 원문을 가져옵니다.",
    parameters={
        "type" : "object",
        "properties" : {
            "chunk_id" : {
                "type" : "string",
                "description" : "검색 결과에 포함된 문단 ID",
            },
        },
        "required" : ["chunk_id"],
    },
)

def find_readable_chunk(chunks : list[dict], chunk_id : str, principal : Principal,) -> dict | None :
    for chunk in chunks:
        if chunk["chunk_id"] != chunk_id:
            continue
        if not can_read_chunk(chunk, principal):
            return None
        return chunk
    return None

class ReadDocumentTool:
    spec = READ_DOC_SPEC

    def __init__(self, doc_path:Path)->None:
        self.document_path = doc_path

    async def run(self, call : ToolCall) -> ToolResult:
        chunk_id = call.arguments.get("chunk_id")

        if not isinstance(chunk_id, str):
            return ToolResult(call.call_id, False, "문단 ID를 문자열로 입력해 주세요.",)

        chunk_id = chunk_id.strip()
        if not chunk_id:
            return ToolResult(call.call_id, False,"문단 ID를 입력해 주세요.")

        try:
            chunks = await asyncio.to_thread(
                load_doc_chunks,
                self.document_path,
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ToolResult(call.call_id, False, "문서 목록을 읽을 수 없습니다.")

        chunk = find_readable_chunk(chunks, chunk_id, call.principal)
        if chunk is None:
            return ToolResult(call.call_id, False, "문단을 찾을 수 없습니다.")

        payload = {
            "doc_id": chunk["doc_id"],
            "chunk_id": chunk["chunk_id"],
            "title": chunk["title"],
            "location": chunk["location"],
            "text": chunk["text"],
        }

        return ToolResult(
            call.call_id,
            True,
            json.dumps(payload, ensure_ascii=False),
            data=payload,
        )
