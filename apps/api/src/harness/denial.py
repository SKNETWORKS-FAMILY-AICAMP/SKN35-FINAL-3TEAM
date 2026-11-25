"""거부·승인 거절·시간 초과는 서버의 확정 문구와 실제 완료 결과로 안내한다."""

from __future__ import annotations

from dataclasses import dataclass
import json

from contracts.harness import Denial

_MAX_RESULT_CHARS = 300


@dataclass(frozen=True)
class DoneTool:
    tool_name: str
    content: str
    name: str = ""  # 도구 코드 이름. tool_name은 화면 표시 이름이다.


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_RESULT_CHARS else text[:_MAX_RESULT_CHARS] + "…"


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _done_summary(done: DoneTool) -> str:
    if done.name == "get_review_case":
        try:
            data = json.loads(done.content)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("status"), str):
            lines = [f"- {done.tool_name}", f"  - 상태: {_clip(data['status'])}"]
            if isinstance(data.get("case_id"), str):
                lines.insert(1, f"  - 심사 건: {_clip(data['case_id'])}")
            missing = data.get("missing_docs")
            if isinstance(missing, list) and all(isinstance(item, str) for item in missing):
                lines.append(f"  - 누락 서류: {_clip(', '.join(missing)) if missing else '없음'}")
            return "\n".join(lines)
    try:
        data = json.loads(done.content)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, (dict, list)):
        return f"- {done.tool_name}: {_clip(done.content)}"
    # JSON 객체 결과는 그대로 붙이지 않는다 (2026-11-25: 고객 이메일·안내문 본문이 거절 안내에 그대로 나왔다)
    if done.name == "prepare_maturity_notices" and isinstance(data, dict) and isinstance(data.get("notices"), list):
        return f"- {done.tool_name}: 보낼 고객 {len(data['notices'])}명 확인"
    if done.name == "send_maturity_notice" and isinstance(data, dict) and data.get("sent"):
        return f"- {done.tool_name}: {_clip(str(data.get('customer', '')))} 고객에게 보냄"
    return f"- {done.tool_name}"


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
        finished = "\n".join(_done_summary(d) for d in done)
        body = f"{body}\n\n완료된 작업:\n{finished}"
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
    """기존 호출부와의 호환용. 실행 하네스는 모델 마무리 대신 template_reply를 사용한다."""
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
