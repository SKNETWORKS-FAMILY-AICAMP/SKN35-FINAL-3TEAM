"""컨텍스트 한도 맞추기 (harness/context.py) 와 LLM 클라이언트의 한도 확인."""

from __future__ import annotations

import pytest

from contracts.harness import LLMToolCall, Message, ToolSpec
from harness import llm as llm_module
from harness.context import estimate_tokens, fit_messages
from tests.test_llm import fake_llm

DOC = "제2조(보존 기간) 여신 관련 서류의 보존 기간은 7년으로 한다.\n" + "담당자는 서류를 확인한다. " * 1500  # 약 2만 자
SYSTEM = Message("system", "규칙")


def call(cid: str, name: str = "parse_doc") -> Message:
    return Message("assistant", "", tool_calls=(LLMToolCall(cid, name, {}),))


def turn(q: str, cid: str, result: str) -> list[Message]:
    return [Message("user", q), call(cid), Message("tool", result, tool_call_id=cid), Message("assistant", "답")]


def test_estimate_is_close_to_measured_and_errs_high():
    # 실측 (qwen3:8b): 한글 위주 문서 20,224자 → 14,730 토큰, 영어 1,800자 → 401 토큰
    korean = "여신 관련 서류의 보존 기간은 칠 년으로 한다 " * 800
    assert 0.9 <= estimate_tokens(korean) / (len(korean) * 0.73) <= 1.4
    assert 401 <= estimate_tokens("The quick brown fox jumps over the lazy dog. " * 40) <= 401 * 2


def test_small_conversation_is_untouched():
    msgs = [SYSTEM, *turn("문서 읽어 줘", "c1", "짧은 결과")]
    out, report = fit_messages(msgs, [], 4096, 1024)
    assert out == msgs and not report.changed


def test_old_tool_results_are_compacted_first():
    msgs = [SYSTEM, *turn("문서 읽어 줘", "c1", DOC), Message("user", "보존 기간은?")]
    assert not fit_messages(msgs, [], 24576, 4096)[1].changed  # 24k 면 그대로 들어간다
    out, report = fit_messages(msgs, [], 8192, 2048)
    assert report.compacted == 1 and report.dropped == 0 and report.truncated == 0
    old = next(m for m in out if m.role == "tool")
    assert old.content.startswith("제2조") and "이전 툴 결과 생략" in old.content
    assert out[-1].content == "보존 기간은?"


def test_oldest_turns_are_dropped_whole_keeping_system_and_current():
    msgs = [SYSTEM]
    for i in range(40):
        msgs += turn(f"질문 {i} " + "가" * 200, f"c{i}", "결과")
    msgs += [Message("user", "마지막 질문"), call("now"), Message("tool", "이번 결과", tool_call_id="now")]
    out, report = fit_messages(msgs, [], 4096, 1024)
    assert report.dropped > 0 and out[0] == SYSTEM
    assert out[-3:] == msgs[-3:]
    # 남은 툴 결과는 모두 자기 호출과 짝이 맞는다
    called = {c.call_id for m in out for c in m.tool_calls}
    assert all(m.tool_call_id in called for m in out if m.role == "tool")
    assert out[1].role == "user"  # 질문 단위로만 뺀다


def test_current_doc_is_cut_with_a_note_instead_of_silently_dropped():
    msgs = [SYSTEM, Message("user", "첨부 문서 읽어 줘"), call("c1"), Message("tool", DOC, tool_call_id="c1")]
    out, report = fit_messages(msgs, [], 4096, 1024)
    assert report.truncated == 1 and report.after <= report.budget
    doc = out[-1]
    assert doc.content.startswith("제2조(보존 기간)")  # 앞부분은 남는다 (Ollama 는 통째로 버렸다)
    assert "길이 한도 때문에 앞부분만 전달함" in doc.content


def test_tool_specs_count_against_the_budget():
    big = [ToolSpec(f"t{i}", "설명 " * 100) for i in range(20)]
    msgs = [SYSTEM, Message("user", "가" * 2000)]
    assert not fit_messages(msgs, [], 4096, 1024)[1].changed
    assert fit_messages(msgs, big, 4096, 1024)[1].budget < fit_messages(msgs, [], 4096, 1024)[1].budget


async def test_client_trims_before_sending():
    seen: list = []
    llm = fake_llm({"content": "7년"}, seen)
    llm._context = 4096
    await llm.chat([SYSTEM, Message("user", "읽어 줘"), call("c1"), Message("tool", DOC, tool_call_id="c1")], [])
    sent = seen[0]["messages"][-1]["content"]
    assert sent.startswith("제2조") and len(sent) < len(DOC) and "앞부분만 전달" in sent


class _FakeHTTP:
    def __init__(self, payload):
        self.payload = payload

    def __call__(self, timeout):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json):
        payload = self.payload

        class R:
            def json(self_inner):
                return payload

        return R()


@pytest.mark.parametrize("payload, expected", [
    ({"parameters": "temperature 0.6\nnum_ctx                        24576\ntop_k 20"}, 24576),
    ({"parameters": "temperature 0.6"}, 4096),  # Modelfile 에 num_ctx 가 없으면 Ollama 기본값
    ({"error": "model 'qwen3:8b-24k' not found"}, 4096),
])
async def test_context_limit_is_read_from_ollama(monkeypatch, payload, expected):
    monkeypatch.setattr(llm_module.httpx, "AsyncClient", _FakeHTTP(payload))
    llm = llm_module.OpenAICompatLLM(base_url="http://localhost:11434/v1", model="qwen3:8b-24k", client=object())
    assert await llm.context_limit() == expected


def test_env_override_and_default_chat_model(monkeypatch):
    from services.container import CHAT_MODEL, FallbackLLM, llm_from_env

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    llm = llm_from_env()
    assert isinstance(llm, FallbackLLM) and llm.primary.model == CHAT_MODEL and llm.fallback.model == "qwen3:8b"
    monkeypatch.setenv("LLM_MODEL", "other")
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "8192")
    llm = llm_from_env()
    assert llm.model == "other" and llm._context == 8192
