"""harness.llm 테스트. 가짜 HTTP 응답으로 변환을 확인하고,
RUN_OLLAMA_TESTS=1 이면 로컬 Ollama 로 실제 호출도 해 본다.
"""

from __future__ import annotations

import json
import os

import httpx2 as httpx  # openai 3.x 는 httpx2 를 쓴다
import pytest
from openai import AsyncOpenAI

from contracts.harness import LLMClient, LLMToolCall, Message, ToolSpec
from harness.llm import OpenAICompatLLM, parse_arguments, strip_thinking


def fake_llm(reply: dict, seen: list) -> OpenAICompatLLM:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", **reply},
                         "finish_reason": "tool_calls" if reply.get("tool_calls") else "stop"}],
        })

    client = AsyncOpenAI(base_url="http://test/v1", api_key="k",
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return OpenAICompatLLM(base_url="unused", model="m", client=client)


SEARCH = ToolSpec("search", "문서 검색", {"type": "object", "properties": {"q": {"type": "string"}}})


async def test_is_llm_client():
    assert isinstance(fake_llm({"content": "hi"}, []), LLMClient)


async def test_request_shape_and_tool_call_parsing():
    seen: list = []
    llm = fake_llm({"content": None, "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": '{"q": "휴가 규정"}'}},
    ]}, seen)
    history = [
        Message("system", "너는 사내 비서"),
        Message("user", "이전 질문"),
        Message("assistant", "", (LLMToolCall("c0", "search", {"q": "a"}),)),
        Message("tool", "결과", tool_call_id="c0"),
        Message("user", "휴가 규정 찾아줘"),
    ]
    resp = await llm.chat(history, [SEARCH])

    body = seen[0]
    assert body["model"] == "m"
    assert body["tools"][0]["function"]["name"] == "search"
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"q": "a"}'
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c0", "content": "결과"}
    assert resp.message.tool_calls == (LLMToolCall("call_1", "search", {"q": "휴가 규정"}),)
    assert resp.message.content == ""
    assert resp.finish_reason == "tool_calls"


async def test_no_tools_param_when_empty_and_thinking_stripped():
    seen: list = []
    llm = fake_llm({"content": "<think>음...</think>\n\n안녕하세요"}, seen)
    resp = await llm.chat([Message("user", "안녕")], [])
    assert "tools" not in seen[0]
    assert resp.message.content == "안녕하세요"


async def test_required_tool_request_is_scoped_to_the_given_tool():
    from contracts.harness import RequiredToolLLMClient

    seen = []
    llm = fake_llm({"content": None, "tool_calls": [
        {"id": "required", "type": "function", "function": {"name": "search", "arguments": '{"q":"test"}'}},
    ]}, seen)
    assert isinstance(llm, RequiredToolLLMClient)
    reply = await llm.chat_required_tool([Message("user", "검색해줘")], SEARCH)
    assert seen[0]["tool_choice"] == {"type": "function", "function": {"name": "search"}}
    assert [item["function"]["name"] for item in seen[0]["tools"]] == ["search"]
    assert reply.message.tool_calls[0].tool_name == "search"
    await llm.chat([Message("user", "일반 질문")], [SEARCH])
    assert "tool_choice" not in seen[1]


def test_parse_arguments_edge_cases():
    assert parse_arguments("") == {}
    assert parse_arguments('{"a": 1}') == {"a": 1}
    assert parse_arguments("{broken") == {"_raw": "{broken"}
    assert parse_arguments("[1, 2]") == {"_raw": "[1, 2]"}
    assert strip_thinking(None) == ""


@pytest.mark.skipif(os.environ.get("RUN_OLLAMA_TESTS") != "1", reason="RUN_OLLAMA_TESTS=1 일 때만")
async def test_live_ollama_tool_call():
    llm = OpenAICompatLLM.from_env(temperature=0)
    resp = await llm.chat(
        [Message("user", "사내 문서에서 '휴가 규정'을 검색해줘. 반드시 search 툴을 써.")], [SEARCH]
    )
    assert resp.message.tool_calls, resp
    assert resp.message.tool_calls[0].tool_name == "search"
    assert "q" in resp.message.tool_calls[0].arguments
