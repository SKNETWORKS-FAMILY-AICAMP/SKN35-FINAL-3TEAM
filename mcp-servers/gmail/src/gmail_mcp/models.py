"""메일 저장 · 동기화 · 툴 입출력 모델.

권한 원칙 (개인 계정 방식)
- 직원마다 **자기 Google 계정**으로 로그인한다. 메일함 주인 = 그 메일을 볼 수 있는 유일한 사람.
- 메일은 **관계형 DB(mail_message)에만** 저장한다. 벡터DB · 청킹 대상이 아니다.
- 모든 조회는 owner_user_id(로그인 사용자) 조건이 붙는다. 저장소(store.py)는 주인 없이 조회하는 함수를 두지 않는다.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

SCHEMA_VERSION = "3.0"  # 2.0(공용 메일함 · 변경 이벤트) → 3.0(개인 메일함 · 관계형 DB 저장)


class MailMessage(BaseModel):
    message_id: str
    thread_id: str = ""
    sender: str = Field(default="", description="From 주소")
    to: list[str] = []
    cc: list[str] = []
    bcc: list[str] = Field(default_factory=list, description="숨은참조 (내가 보낸 메일에만 있음)")
    subject: str = ""
    sent_at: datetime | None = None
    labels: list[str] = []
    body: str = ""
    body_omitted: bool = Field(default=False, description="본문 일부가 너무 커서 생략됨")


class MailRecord(MailMessage):
    """mail_message 테이블의 한 행 = 한 직원 메일함의 메일 한 통."""

    owner_user_id: str
    size_bytes: int = 0
    content_hash: str = Field(description="저장 내용의 해시 — 같으면 다시 쓰지 않는다")


class FailureReason(StrEnum):
    """수집 단계 실패 사유 코드."""

    FETCH_ERROR = "fetch_error"  # Gmail API 호출 실패 (네트워크 · 권한 · 서버 오류)
    TOO_LARGE = "too_large"  # 크기 제한 초과 — 건너뛰지 않고 기록
    INVALID_MESSAGE = "invalid_message"  # ID 없음 등 원본이 비정상


class SyncFailure(BaseModel):
    """메일 한 통의 실패. 전체 동기화를 멈추지 않고 기록만 한다 (Onyx 의 ConnectorFailure)."""

    message_id: str | None
    reason: FailureReason
    detail: str = ""


class DeletedCandidate(BaseModel):
    """목록에는 있었지만 가져오려 할 때 사라진 메일 — 실패가 아니라 삭제 후보."""

    message_id: str


class MailCheckpoint(BaseModel):
    """증분 동기화 재개 지점 (Confluence 커넥터의 next_page_url 에 해당하는 page_token)."""

    has_more: bool = True
    page_token: str | None = None


class ChangeKind(StrEnum):
    NEW = "new"
    UPDATED = "updated"  # 라벨 등 저장 내용이 바뀜
    DELETED = "deleted"


# ── MCP 툴 입출력 ──────────────────────────────────────────


class MailSummary(BaseModel):
    thread_id: str
    subject: str
    sender: str
    sent_at: datetime | None
    message_count: int


class MailSearchResult(BaseModel):
    query: str
    threads: list[MailSummary]
    synced: bool = Field(description="검색 전에 최신 메일을 가져왔는지 (실패하면 저장된 메일로만 검색)")


class MailThread(BaseModel):
    thread_id: str
    subject: str
    link: str
    messages: list[MailMessage]
    truncated: bool = Field(description="본문 일부가 길이 제한으로 잘렸거나 생략됐는지")


class SendResult(BaseModel):
    message_id: str
    thread_id: str
    status: str = Field(default="sent", description="sent(실제 발송) / simulated(가상 메일함, 발송 안 됨)")
    to: list[str]
    cc: list[str] = []
