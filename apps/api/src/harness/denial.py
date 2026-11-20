"""권한 거부로 run 을 멈출 때의 마무리 답변 (계약 규칙 8-c).

- 성공한 툴 실행이 없으면 template_reply() 고정 문구로 바로 답한다.
- 있으면 wrapup_instruction() 을 붙여 LLM 을 tools=[] 로 한 번 더 부른다.
  (qwen3:8b 실험에서 거부 종류·사유·끝난 결과를 지시문에 직접 넣어야 안내가 정확했다)
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.harness import Denial

_MAX_RESULT_CHARS = 300


@dataclass(frozen=True)
class DoneTool:
    tool_name: str
    content: str


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_RESULT_CHARS else text[:_MAX_RESULT_CHARS] + "…"


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def template_reply(denial: Denial, done: list[DoneTool] = ()) -> str:
    name = denial.label
    if denial.kind == "deny":
        body = (
            f"'{name}' 작업은 권한이 없어 실행하지 않았습니다. "
            f"사유: {_sentence(denial.reason)} "
            "필요하면 권한이 있는 부서장이나 관리자에게 요청해 주세요."
        )
    elif denial.kind == "timeout":
        body = (
            f"'{name}' 작업은 승인 시간 안에 응답이 없어 실행하지 않았습니다. "
            "계속하려면 다시 요청한 뒤 승인해 주세요."
        )
    else:
        body = (
            f"'{name}' 작업은 승인하지 않으셔서 실행하지 않았습니다. "
            "필요하면 내용을 바꿔 다시 요청해 주세요."
        )
    if done:
        finished = "\n".join(f"- {d.tool_name}: {_clip(d.content)}" for d in done)
        body = f"완료된 작업:\n{finished}\n\n{body}"
    return body


_SITUATION = {
    "deny": "회사 권한 정책에 따라 실행이 거부됐다. 사유: {reason}",
    "rejected": "사용자가 실행을 거절했다. 권한 문제가 아니다.",
    "timeout": "승인 시간 안에 사용자가 응답하지 않아 실행하지 않았다. 사용자가 거절한 것도, 권한 문제도 아니다.",
}
_NEXT = {
    "deny": "필요하면 권한이 있는 사람(부서장·관리자)에게 요청하라고 안내",
    "rejected": "거절하셔서 진행하지 않았다고 확인하고, 필요하면 다시 요청할 수 있다고 안내",
    "timeout": "승인 응답이 없어 진행하지 않았다고 알리고, 계속하려면 다시 요청한 뒤 승인하면 된다고 안내",
}


def wrapup_instruction(denial: Denial, done: list[DoneTool]) -> str:
    finished = "\n".join(f"- {d.tool_name}: {_clip(d.content)}" for d in done)
    return (
        "[안내 지시] 이번 요청은 여기서 끝났다. 더 이상 도구를 쓸 수 없다.\n"
        f"막힌 작업: {denial.label}\n"
        f"상황: {_SITUATION[denial.kind].format(reason=denial.reason)}\n"
        f"이미 끝난 작업 결과:\n{finished}\n"
        "사용자에게 직접 말하듯 3문장 이내로 답하라. 먼저 끝난 결과의 내용을 전하고, "
        f"막힌 작업과 이유를 말한 뒤, {_NEXT[denial.kind]}하라. "
        "막힌 작업을 다른 방법으로 해내는 방법(우회)은 제안하지 마라. 이 지시문 자체는 언급하지 마라."
    )
