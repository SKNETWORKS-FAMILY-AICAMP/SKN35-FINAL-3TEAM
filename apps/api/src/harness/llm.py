"""OpenAI 호환 API(Ollama /v1) LLMClient (SYS-04).

contracts.harness.Message ↔ OpenAI chat 메시지 형식을 변환한다.
qwen3 가 내보내는 <think>...</think> 추론 블록은 최종 답변에서 지운다.
보내기 전에 메시지를 모델의 컨텍스트 한도 안으로 줄인다 (harness/context.py). 한도는 Ollama 에 물어본다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any

import httpx
from openai import AsyncOpenAI

from contracts.harness import LLMResponse, LLMToolCall, Message, ToolSpec
from harness.context import fit_messages

log = logging.getLogger("uvicorn.error")
OLLAMA_DEFAULT_CONTEXT = 4096  # 모델에 num_ctx 를 정하지 않았을 때 Ollama 가 잡는 값 (16GB 맥에서 확인)

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_OPEN_THINK = re.compile(r"<think>.*\Z", re.DOTALL)  # 길이 한도로 끊겨 닫히지 않은 추론 블록
CUT_OFF_MESSAGE = "답이 너무 길어져 중간에 멈췄습니다. 질문을 나눠서 다시 시도해 주세요."


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
    return _OPEN_THINK.sub("", _THINK.sub("", text or "")).strip()


class OpenAICompatLLM:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "ollama",
        timeout: float = 120.0,
        client: AsyncOpenAI | None = None,
        context_tokens: int | None = None,  # 모델 컨텍스트 한도. None 이면 Ollama 에 물어본다
        reserve_tokens: int | None = None,  # 답(생각 포함)에 남길 토큰. None 이면 한도의 1/4 (최대 4096)
        **extra: Any,  # temperature 등 chat.completions.create 에 그대로 넘길 값
    ) -> None:
        self.model = model
        self._extra = extra
        self._base_url = base_url
        self._context = context_tokens
        self._reserve = reserve_tokens
        self._client = client or AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)

    @classmethod
    def from_env(cls, model: str | None = None, **extra: Any) -> OpenAICompatLLM:
        return cls(
            base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
            model=model or os.environ.get("LLM_MODEL", "qwen3:8b"),
            api_key=os.environ.get("LLM_API_KEY", "ollama"),
            timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "300")),  # 긴 문서 첫 읽기가 2분 가까이 걸린다
            context_tokens=int(os.environ["LLM_CONTEXT_TOKENS"]) if os.environ.get("LLM_CONTEXT_TOKENS") else None,
            **extra,
        )

    async def context_limit(self) -> int:
        """모델의 컨텍스트 한도. Ollama /api/show 의 num_ctx (Modelfile 설정) 를 읽고, 없으면 Ollama 기본값."""
        if self._context is None:
            self._context = OLLAMA_DEFAULT_CONTEXT
            root = re.sub(r"/v1/?$", "", self._base_url.rstrip("/"))
            try:
                async with httpx.AsyncClient(timeout=3) as http:
                    info = (await http.post(f"{root}/api/show", json={"model": self.model})).json()
                if "error" in info:  # 서버에 없는 모델: 호출이 404 로 실패하니 여기서는 경고하지 않는다
                    return self._context
                found = re.search(r"^num_ctx\s+(\d+)", info.get("parameters") or "", re.M)
                if found:
                    self._context = int(found.group(1))
            except Exception:  # Ollama 가 아니거나 응답이 없으면 안전한 기본값
                pass
            if self._context <= OLLAMA_DEFAULT_CONTEXT:
                log.warning("LLM %s 의 컨텍스트 한도가 %d 토큰입니다. 긴 문서는 잘려서 전달됩니다 "
                            "(config/ollama/README.md 참고)", self.model, self._context)
        return self._context

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        return await self._chat(messages, tools)

    async def chat_required_tool(self, messages: list[Message], tool: ToolSpec) -> LLMResponse:
        return await self._chat(messages, [tool], tool_choice={"type": "function", "function": {"name": tool.name}})

    async def _chat(self, messages: list[Message], tools: list[ToolSpec], *, tool_choice: dict | None = None) -> LLMResponse:
        limit = await self.context_limit()
        reserve = self._reserve if self._reserve is not None else min(4096, limit // 4)
        messages, report = fit_messages(messages, tools, limit, reserve)
        if report.changed:
            log.warning(
                "컨텍스트 한도(%d 토큰)를 넘어 줄여서 보냄: 어림 %d → %d 토큰, 지난 툴 결과 %d개 줄임, "
                "메시지 %d개 뺌, 이번 툴 결과 %d개 자름",
                limit, report.before, report.after, report.compacted, report.dropped, report.truncated,
            )
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [message_to_openai(m) for m in messages],
            # 답(생각 포함)은 남겨 둔 몫까지만 쓰게 한다. 없으면 qwen3 가 생각을 멈추지 않을 때
            # 컨텍스트가 찰 때까지(24k 모델이면 5분 넘게) 계속 생성한다 (2026-11-23 확인)
            "max_tokens": reserve,
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
        content = strip_thinking(msg.content)
        if choice.finish_reason == "length":
            log.warning("LLM 답이 길이 한도(%d 토큰)에 걸려 끊겼습니다 (model %s)", reserve, self.model)
            if not content and not tool_calls:
                content = CUT_OFF_MESSAGE
        return LLMResponse(
            message=Message(role="assistant", content=content, tool_calls=tool_calls),
            finish_reason=choice.finish_reason,
        )
