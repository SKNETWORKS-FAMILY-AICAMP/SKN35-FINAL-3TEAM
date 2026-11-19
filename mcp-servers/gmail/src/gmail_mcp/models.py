"""메일 동기화 · 권한 동기화 · 툴 입출력 모델.

용어는 Onyx 커넥터 구조를 따른다 (Document / SlimDocument / Checkpoint / ConnectorFailure / ExternalAccess).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1.0"


class MailAccess(BaseModel):
    """메일 문서를 볼 수 있는 사람 (Onyx 의 ExternalAccess 에 해당).

    팀 공용 메일함 하나를 쓰므로, 메일함 소유자(팀 계정)만이 아니라
    **메일 참여자(From·To·Cc·Bcc)** 를 열람 가능자로 둔다. 플랫폼은 이 이메일을 users 표의 직원과 매칭한다.
    """

    model_config = ConfigDict(frozen=True)

    user_emails: tuple[str, ...]
    group_ids: tuple[str, ...] = ()
    is_public: bool = False

    @field_validator("user_emails", "group_ids", mode="before")
    @classmethod
    def _normalize(cls, values):
        return tuple(sorted({v.strip().lower() for v in values if v and v.strip()}))

    def allows(self, email: str | None) -> bool:
        return self.is_public or (email is not None and email.strip().lower() in self.user_emails)


class MailMessage(BaseModel):
    message_id: str
    sender: str = Field(description="From 주소")
    to: list[str] = []
    cc: list[str] = []
    bcc: list[str] = []
    subject: str = ""
    sent_at: datetime | None = None
    labels: list[str] = []
    body: str = ""

    @property
    def participants(self) -> set[str]:
        return {a for a in [self.sender, *self.to, *self.cc, *self.bcc] if a}


class MailDocument(BaseModel):
    """동기화 결과 1건 = 스레드 1개 (Onyx 와 같이 스레드 단위 문서). doc_id 가 같으면 덮어쓴다(멱등)."""

    schema_version: str = SCHEMA_VERSION
    doc_id: str = Field(description="'gmail:<thread_id>'")
    thread_id: str
    source: str = "gmail"
    title: str
    link: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    primary_owners: list[str] = Field(default_factory=list, description="보낸 사람")
    secondary_owners: list[str] = Field(default_factory=list, description="받는 사람 (To·Cc·Bcc)")
    messages: list[MailMessage]
    access: MailAccess

    def to_text(self) -> str:
        """파서·청커에 넘길 본문 텍스트 (메시지 순서대로)."""
        parts = []
        for m in self.messages:
            header = f"From: {m.sender}\nTo: {', '.join(m.to)}\nDate: {m.sent_at or ''}\nSubject: {m.subject}"
            parts.append(f"{header}\n\n{m.body}".strip())
        return "\n\n---\n\n".join(parts)


class SlimMailDocument(BaseModel):
    """본문 없이 ID + 권한만 (권한 동기화 · 삭제 감지용)."""

    doc_id: str
    access: MailAccess


class SyncFailure(BaseModel):
    """문서 하나의 실패. 전체 동기화를 멈추지 않고 기록만 한다 (Onyx 의 ConnectorFailure)."""

    doc_id: str | None
    link: str | None = None
    error: str


class MailCheckpoint(BaseModel):
    """증분 동기화 재개 지점 (Confluence 커넥터의 next_page_url 에 해당하는 page_token)."""

    has_more: bool = True
    page_token: str | None = None


class DocAccessUpdate(BaseModel):
    """권한 동기화 결과 1건 (Onyx 의 DocExternalAccess)."""

    doc_id: str
    access: MailAccess


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


class MailThread(BaseModel):
    thread_id: str
    subject: str
    link: str
    messages: list[MailMessage]
    truncated: bool = Field(description="본문 일부가 길이 제한으로 잘렸는지")


class SendResult(BaseModel):
    message_id: str
    thread_id: str
    status: str = Field(default="sent", description="sent(실제 발송) / simulated(가상 메일함, 발송 안 됨)")
    to: list[str]
    cc: list[str] = []
