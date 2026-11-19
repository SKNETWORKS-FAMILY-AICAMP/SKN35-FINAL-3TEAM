"""계약 테스트 — 서버가 실제로 노출하는 툴과 manifest.yaml 이 일치하는지."""

from pathlib import Path

import yaml
from mcp import Client

from gmail_mcp.fake import FakeGmailClient, sample_mailbox
from gmail_mcp.server import build_server
from gmail_mcp.service import MailService

MANIFEST = yaml.safe_load((Path(__file__).resolve().parents[1] / "manifest.yaml").read_text(encoding="utf-8"))


def server():
    return build_server(MailService(sample_mailbox(FakeGmailClient())))


async def list_tools():
    async with Client(server()) as c:
        return {t.name: t for t in (await c.list_tools()).tools}


async def test_tools_match_manifest():
    assert set(await list_tools()) == {t["name"] for t in MANIFEST["tools"]}


async def test_injected_arguments_exist_and_are_required():
    tools = await list_tools()
    for policy in MANIFEST["tools"]:
        schema = tools[policy["name"]].input_schema
        for arg in policy.get("inject", {}):
            assert arg in schema.get("required", []), f"{policy['name']}: {arg} 는 필수 인자여야 합니다"


async def test_every_tool_documents_itself():
    for tool in (await list_tools()).values():
        assert tool.description and len(tool.description) >= 15
        assert tool.output_schema
        for name, prop in tool.input_schema["properties"].items():
            assert prop.get("description"), f"{tool.name}.{name}: 인자 설명이 없습니다"


async def test_permission_error_is_tool_error():
    async with Client(server()) as c:
        r = await c.call_tool("read_mail", {"requester_email": "nobody@acme.example", "thread_id": "18c1a0000000a001"})
        assert r.is_error and "권한" in r.content[0].text
