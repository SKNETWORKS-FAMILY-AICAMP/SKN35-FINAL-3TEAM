"""계약 테스트 — 서버가 실제로 노출하는 툴과 manifest.yaml 이 일치하는지."""

from pathlib import Path

import pytest
import yaml
from mcp import Client

from gmail_mcp.server import build_server

MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / "manifest.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def server(svc):
    return build_server(svc)


async def list_tools(server):
    async with Client(server) as c:
        return {t.name: t for t in (await c.list_tools()).tools}


async def test_tools_match_manifest(server):
    assert set(await list_tools(server)) == {t["name"] for t in MANIFEST["tools"]}


async def test_every_tool_injects_requester_id_only(server):
    tools = await list_tools(server)
    for policy in MANIFEST["tools"]:
        assert policy["inject"] == {"requester_id": "user_id"}, f"{policy['name']}: 요청자는 사번으로만 식별"
        schema = tools[policy["name"]].input_schema
        assert "requester_id" in schema["required"]
        assert "requester_email" not in schema["properties"]  # 이메일은 이제 받지 않음


async def test_every_tool_documents_itself(server):
    for tool in (await list_tools(server)).values():
        assert tool.description and len(tool.description) >= 15
        assert tool.output_schema
        for name, prop in tool.input_schema["properties"].items():
            assert prop.get("description"), f"{tool.name}.{name}: 인자 설명이 없습니다"


async def test_other_users_thread_is_tool_error(server):
    async with Client(server) as c:
        r = await c.call_tool("read_mail", {"requester_id": "E1001", "thread_id": "ct1"})
        assert r.is_error and "권한" in r.content[0].text
