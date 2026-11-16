"""LLMClient 구현: OpenAI 호환 API (기본값은 Ollama /v1).

중요: 우리 Message.to_dict() 는 '저장용' 형식이다. OpenAI 형식(tool_calls[].function.arguments 는
JSON 문자열)과 다르므로, API 변환은 이 파일 안에서만 한다.
"""
from __future__ import annotations

import json
import os
from typing import Any

from contracts.harness import LLMResponse, LLMToolCall, Message, ToolSpec

# 우리 메시지를 AI가 이해할 형식으로 바꿈
def message_to_api(m: Message) -> dict[str, Any]:
    d: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        d["tool_calls"] = [
            {
                "id": c.call_id,
                "type": "function",
                "function": {
                    "name": c.tool_name,
                    "arguments": json.dumps(c.arguments, ensure_ascii=False),
                },
            }
            for c in m.tool_calls
        ]
    if m.role == "tool":
        d["tool_call_id"] = m.tool_call_id
    return d

# Tool 설명을 AI에게 전달할 형식으로 바꿈
def tool_to_api(t: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
    }

# AI 응답을 우리 프로그램 형식으로 바꿈
def message_from_api(msg: Any) -> Message:
    calls = []
    for tc in msg.tool_calls or []:
        raw = tc.function.arguments or "{}"
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}  # 모델이 깨진 JSON 을 줬다: 툴이 필수 인자 누락 에러를 돌려주면 모델이 재시도한다
        calls.append(LLMToolCall(call_id=tc.id, tool_name=tc.function.name, arguments=args))
    return Message(role="assistant", content=msg.content or "", tool_calls=tuple(calls))

# 실제 AI 서버와 통신하는 클래스
class OpenAICompatClient:
    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.7,
        client: Any = None,  # 테스트용 주입
    ) -> None:
        self.model = model or os.environ.get("LLM_MODEL") or "llama3.1"  # 툴 호출을 지원하는 모델이어야 함
        self.temperature = temperature
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                base_url=base_url or os.environ.get("LLM_BASE_URL") or "http://localhost:11434/v1",
                api_key=api_key or os.environ.get("LLM_API_KEY") or "ollama",  # Ollama 는 아무 값이나 가능
            )
        self._client = client

    def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = [tool_to_api(t) for t in tools]
            kwargs["tool_choice"] = "auto"
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[message_to_api(m) for m in messages],
            temperature=self.temperature,
            **kwargs,
        )
        choice = resp.choices[0]
        return LLMResponse(message=message_from_api(choice.message), finish_reason=choice.finish_reason)
