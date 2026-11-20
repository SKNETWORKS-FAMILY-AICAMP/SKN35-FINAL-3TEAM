import asyncio
from pathlib import Path

from apps.api.src.tools.search_doc import SearchDocumentsTool
from contracts.permission import Principal, ToolCall


async def main():
    tool = SearchDocumentsTool(
        Path("data/fixtures/document_chunks.json")
    )

    principal = Principal(
        user_id="kim",
        roles=("member",),
        attributes={"dept_id": 10, "security_grade": 2},
    )

    call = ToolCall(
        call_id="search-test-001",
        tool_name="search_doc",
        arguments={"query": "서류 반려"},
        principal=principal,
        session_id="search-test-session",
    )

    result = await tool.run(call)
    print("성공 여부:", result.ok)
    print("검색 결과:", result.content)


asyncio.run(main())