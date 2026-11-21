"""구체적인 심사 건 조회는 이번 요청의 실제 도구 결과가 있을 때만 답한다."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

from contracts.harness import LLMToolCall, Message

LOOKUP_TOOL = "get_review_case"
SUBMISSION_TOOL = "submit_review_request"
_CASE_ID = re.compile(r"(?<![A-Za-z0-9_-])REVIEW-\d+(?![A-Za-z0-9_-])", re.I)
_LOOKUP_REQUEST = re.compile(r"조회|확인|상태|누락|서류|메모|등록|심사|알려|찾아")
_EXPLANATION = re.compile(r"사용법|코드|프롬프트|(?:조회|호출|사용|등록)\s*(?:예시|샘플|예제)")
_FOLLOWUP = re.compile(r"다시\s*(?:조회|확인)|재조회|(?:그|이|해당|앞서|방금).*(?:건|요청|심사).*(?:조회|확인|상태|누락|메모)")
_REGISTER_REQUEST = re.compile(r"(?:등록|접수|신청)\s*(?:을|를)?\s*(?:해|하(?:자|고|겠|려)|진행(?:해|하)|요청(?:해|하)|부탁(?:해|드려))")
_NO_REGISTRATION = re.compile(r"조회만|확인만|설명만|(?:등록|접수|신청).{0,10}(?:하지|말고|금지|취소|않|안\s*해)")
_QUOTED_NOTES = re.compile(r'''메모(?:를|는|:)?\s*(?:“([^”]*)”|"([^"]*)"|'([^']*)'|‘([^’]*)’)''')
_REUSE_NOTES = re.compile(r"기존.{0,15}(?:동일|같은)|(?:동일|같은).{0,6}(?:조건|메모)")


def _payload(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


@dataclass
class ReviewLookupGuard:
    case_ids: tuple[str, ...]
    registration_requested: bool = False
    retried: bool = False
    records: dict[str, dict] = field(default_factory=dict)
    submissions: dict[str, dict] = field(default_factory=dict)
    submission_attempted: set[str] = field(default_factory=set)
    submission_retry_requested: bool = False
    requested_notes: str | None = None
    notes_specified: bool = False
    reuse_existing_notes: bool = False

    @classmethod
    def from_request(cls, question: str, history: list[Message]) -> ReviewLookupGuard | None:
        # 사용법·코드 설명을 실제 업무 데이터 조회로 바꾸지 않는다.
        if _EXPLANATION.search(question) and not re.search(r"실제(?:로)?|지금.*(?:조회|확인)", question):
            return None
        if not _LOOKUP_REQUEST.search(question):
            return None
        ids = tuple(dict.fromkeys(value.upper() for value in _CASE_ID.findall(question)))
        registration_requested = bool(_REGISTER_REQUEST.search(question)) and not _NO_REGISTRATION.search(question)
        if not ids and (_FOLLOWUP.search(question) or registration_requested):
            calls = {call.call_id: call for message in history for call in message.tool_calls if call.tool_name == LOOKUP_TOOL}
            for message in reversed(history):
                if message.role != "tool" or message.tool_call_id not in calls:
                    continue
                data = _payload(message.content)
                call = calls[message.tool_call_id]
                if data and isinstance(data.get("case_id"), str) and isinstance(data.get("status"), str):
                    if data["case_id"] == call.arguments.get("case_id"):
                        ids = (data["case_id"],)
                        break
        matches = list(_QUOTED_NOTES.finditer(question))
        notes = next(value for value in matches[0].groups() if value is not None).strip() if len(matches) == 1 else None
        if notes is not None and len(notes) > 1000:
            notes = None
        return cls(ids, registration_requested, requested_notes=notes, notes_specified=bool(matches),
                   reuse_existing_notes=bool(_REUSE_NOTES.search(question))) if ids else None

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(case_id for case_id in self.case_ids if case_id not in self.records)

    def nudge(self) -> Message:
        return Message("user", (
            "[심사 조회 확인] 방금 작성한 답변은 실제 조회 결과가 없어 사용할 수 없다. "
            f"지금은 {LOOKUP_TOOL}만 호출하여 다음 심사 건을 각각 조회하라: {', '.join(self.missing)}. "
            "등록하거나 결과를 추측하지 말고 도구를 호출하라."
        ))

    def pending_submissions(self) -> tuple[str, ...]:
        if not self.registration_requested or self.missing or self.submission_retry_requested:
            return ()
        return tuple(case_id for case_id in self.case_ids
                     if case_id not in self.submissions and case_id not in self.submission_attempted)

    def submission_nudge(self, case_ids: tuple[str, ...]) -> Message:
        return Message("user", (
            "[심사 등록 진행] 사용자가 심사 요청 등록을 명시적으로 요청했고 심사 건 조회는 성공했다. "
            f"다음 심사 건에 대해 지금은 {SUBMISSION_TOOL}을 호출하라: {', '.join(case_ids)}. "
            "계획이나 조회 결과만 반복하지 마라. 메모는 사용자의 이번 요청에서 지정한 값을 사용한다. "
            "사용자가 기존과 동일한 조건을 요청했으면 방금 조회한 등록 요청의 메모를 사용한다. "
            "이미 등록된 요청이 있어도 동일 조건의 재등록은 기존 요청을 반환하는 정상 동작이다. "
            "이전 요청에서 다른 메모로 실패했다는 이유로 이번 동일 조건 요청을 거절하지 마라. "
            "메모를 임의로 만들어 바꾸지 마라. 실제 등록은 기존 권한 검사와 사용자 승인 후에만 실행된다."
            "\n\n[이번 요청에서 실제 조회한 자료]\n"
            + json.dumps({case_id: self.records[case_id] for case_id in case_ids}, ensure_ascii=False)
        ))

    def submission_messages(self, messages: list[Message], case_ids: tuple[str, ...]) -> list[Message]:
        # 이전의 실패·완료 답변을 이번 등록의 제약으로 오인하지 않도록 단계 입력만 좁힌다.
        current_request = next(message for message in reversed(messages) if message.role == "user")
        return [*(message for message in messages if message.role == "system"),
                current_request, self.submission_nudge(case_ids)]

    def submission_calls(self, reply: Message, case_ids: tuple[str, ...]) -> tuple[LLMToolCall, ...]:
        calls, seen = [], set()
        for call in reply.tool_calls:
            case_id = call.arguments.get("case_id")
            if isinstance(case_id, str):
                case_id = case_id.strip().upper()
            if (call.tool_name == SUBMISSION_TOOL and isinstance(case_id, str) and case_id in case_ids
                    and case_id not in self.submission_attempted and case_id not in seen):
                arguments = {**call.arguments, "case_id": case_id}
                trusted_notes = self.trusted_submission_notes(case_id)
                if trusted_notes is not None:
                    arguments["notes"] = trusted_notes
                calls.append(LLMToolCall(call.call_id, call.tool_name, arguments))
                seen.add(case_id)
        for case_id in case_ids:
            notes = self.trusted_submission_notes(case_id)
            if (self.registration_requested and case_id in self.records and case_id not in seen
                    and case_id not in self.submission_attempted and notes is not None):
                calls.append(LLMToolCall(f"review_submit_{uuid.uuid4().hex[:12]}", SUBMISSION_TOOL,
                                         {"case_id": case_id, "notes": notes}))
        return tuple(calls)

    def trusted_submission_notes(self, case_id: str) -> str | None:
        # 한 건의 명시적인 등록 지시와 확정된 메모만 연결한다. 모호한 여러 건·메모는 추측하지 않는다.
        if not self.registration_requested or len(self.case_ids) != 1 or case_id not in self.records:
            return None
        if self.notes_specified:
            return self.requested_notes
        if self.reuse_existing_notes:
            request = self.records[case_id].get("review_request")
            if isinstance(request, dict) and isinstance(request.get("notes"), str) and len(request["notes"]) <= 1000:
                return request["notes"]
        return None

    def retry_calls(self, reply: Message) -> tuple[LLMToolCall, ...]:
        # 재시도는 요청한 건의 읽기만 허용한다. 등록·발송을 대신 실행하지 않는다.
        calls = tuple(call for call in reply.tool_calls
                      if call.tool_name == LOOKUP_TOOL and call.arguments.get("case_id") in self.missing)
        called = {call.arguments["case_id"] for call in calls}
        # 모델이 다시 설명만 하거나 일부 건을 누락해도, 명시된 건만 정상 실행 경로로 조회한다.
        fallback = tuple(LLMToolCall(f"review_{uuid.uuid4().hex[:12]}", LOOKUP_TOOL, {"case_id": case_id})
                         for case_id in self.missing if case_id not in called)
        return calls + fallback

    def record(self, name: str, arguments: dict, content: str) -> None:
        data = _payload(content)
        if not data or data.get("case_id") not in self.case_ids or data.get("case_id") != arguments.get("case_id"):
            return
        if name == LOOKUP_TOOL and isinstance(data.get("status"), str):
            self.records[data["case_id"]] = data
        elif name == SUBMISSION_TOOL and data.get("status") == "registered":
            self.submissions[data["case_id"]] = data

    def unavailable(self, *, tool_missing: bool = False) -> str:
        if tool_missing:
            return "이 에이전트에는 심사 건 조회 도구가 없어 현재 상태와 누락 서류, 등록 내역을 확인할 수 없습니다."
        return "요청한 심사 건을 실제로 조회하지 못해 상태와 누락 서류, 등록 내역을 확인할 수 없습니다. 조회되지 않은 내용은 안내하지 않겠습니다."

    def summary(self) -> str:
        lines = ["심사 건 조회 결과입니다."]
        for case_id in self.case_ids:
            data = self.records[case_id]
            lines += [f"\n심사 건: {case_id}", f"- 상태: {data['status']}"]
            for key, label in (("submitted_documents", "제출 서류"), ("missing_documents", "누락 서류")):
                values = data.get(key)
                if isinstance(values, list) and all(isinstance(value, str) for value in values):
                    lines.append(f"- {label}: {', '.join(values) if values else '없음'}")
            request = self.submissions.get(case_id) or data.get("review_request")
            if isinstance(request, dict):
                lines.append("- 심사 요청: 등록됨" if request.get("status") == "registered" else "- 심사 요청: 조회된 등록 정보 있음")
                if isinstance(request.get("request_id"), str):
                    lines.append(f"- 요청 번호: {request['request_id']}")
                if isinstance(request.get("notes"), str):
                    lines.append(f"- 메모: {request['notes']}")
                if case_id in self.submissions and request.get("already_registered") is True:
                    lines.append("- 재등록 결과: 이미 등록된 요청을 반환했습니다. 중복 생성하지 않았습니다.")
            elif "review_request" in data:
                lines.append("- 등록 전 조회한 심사 요청: 없음" if case_id in self.submission_attempted else "- 등록된 심사 요청: 없음")
            if self.registration_requested and case_id not in self.submissions:
                if case_id in self.submission_attempted:
                    lines.append("- 등록을 시도했지만 성공을 확인하지 못했습니다. 자동 재시도하지 않았으므로 실행 결과를 확인한 뒤 다시 요청해 주세요.")
                else:
                    lines.append("- 이번 요청에서 심사 요청 등록은 실행되지 않았습니다.")
        return "\n".join(lines)
