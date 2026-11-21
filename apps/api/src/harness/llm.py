"""OpenAI 호환 API(Ollama /v1) LLMClient (SYS-04).

contracts.harness.Message ↔ OpenAI chat 메시지 형식을 변환한다.
qwen3 가 내보내는 <think>...</think> 추론 블록은 최종 답변에서 지운다.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from openai import AsyncOpenAI

from contracts.harness import LLMResponse, LLMToolCall, Message, ToolSpec

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def message_to_openai(m: Message) -> dict[str, Any]:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.role == "assistant" and m.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.call_id,
                "type": "function",
                "function": {"name": tc.tool_name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
            }
            for tc in m.tool_calls
        ]
    return out


def spec_to_openai(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": spec.name, "description": spec.description, "parameters": spec.parameters},
    }


def parse_arguments(raw: str | None) -> dict[str, Any]:
    """모델이 깨진 JSON 을 줄 때가 있다. 그때는 원문을 _raw 로 넘겨 툴이 에러로 돌려주게 한다."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_raw": raw}


def strip_thinking(text: str | None) -> str:
    return _THINK.sub("", text or "").strip()


class OpenAICompatLLM:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "ollama",
        timeout: float = 120.0,
        client: AsyncOpenAI | None = None,
        **extra: Any,  # temperature 등 chat.completions.create 에 그대로 넘길 값
    ) -> None:
        self.model = model
        self._extra = extra
        self._client = client or AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)

    @classmethod
    def from_env(cls, **extra: Any) -> OpenAICompatLLM:
        return cls(
            base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
            model=os.environ.get("LLM_MODEL", "qwen3:8b"),
            api_key=os.environ.get("LLM_API_KEY", "ollama"),
            timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "120")),
            **extra,
        )

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        return await self._chat(messages, tools)

    async def chat_required_tool(self, messages: list[Message], tool: ToolSpec) -> LLMResponse:
        return await self._chat(messages, [tool], tool_choice={"type": "function", "function": {"name": tool.name}})

    async def _chat(self, messages: list[Message], tools: list[ToolSpec], *, tool_choice: dict | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [message_to_openai(m) for m in messages],
            **self._extra,
        }
        if tools:
            kwargs["tools"] = [spec_to_openai(t) for t in tools]
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        resp = await self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = tuple(
            LLMToolCall(
                call_id=tc.id or f"call_{uuid.uuid4().hex[:12]}",
                tool_name=tc.function.name,
                arguments=parse_arguments(tc.function.arguments),
            )
            for tc in (msg.tool_calls or [])
        )
        return LLMResponse(
            message=Message(role="assistant", content=strip_thinking(msg.content), tool_calls=tool_calls),
            finish_reason=choice.finish_reason,
        )
