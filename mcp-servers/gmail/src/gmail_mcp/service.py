"""메일 툴의 업무 로직 — 권한 확인 · 입력 검증. MCP와 무관하게 단위 테스트한다.

권한 원칙 (개인 계정 방식, 요구사항 SYS-03 "권한 필터는 툴 내부에서 강제, LLM이 고르지 않음")
- requester_id 는 LLM이 아니라 플랫폼이 로그인 사용자 사번으로 채운다 (manifest 의 inject)
- 검색 · 읽기는 관계형 DB 에서 **owner_user_id = 요청자** 인 메일만 본다. 다른 직원의 메일은 thread_id 를 알아도 열 수 없다
- 발송은 요청자 **본인 계정**으로 한다
"""

from __future__ import annotations

import base64
import logging
import re
from email.message import EmailMessage

from .accounts import ClientFactory
from .config import Settings
from .convert import NO_SUBJECT, thread_link
from .google_auth import USER_ID_RE, AccountNotConnected
from .models import MailSearchResult, MailSummary, MailThread, SendResult
from .runner import run_incremental
from .store import MailStore
from .sync import GmailSyncConnector

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
NOT_FOUND = "메일을 찾을 수 없거나 열람 권한이 없습니다"  # 존재 여부를 드러내지 않도록 같은 문구 사용
NOT_CONNECTED = "메일 계정이 연결되지 않았습니다. 관리자에게 메일 계정 연결을 요청하세요"


class InvalidRequest(ValueError):
    """사용자(에이전트)에게 그대로 보여줘도 되는 입력 오류."""


class MailService:
    def __init__(self, store: MailStore, clients: ClientFactory, settings: Settings | None = None) -> None:
        self.store = store
        self.clients = clients
        self.settings = settings or Settings()

    @staticmethod
    def _owner(requester_id: str) -> str:
        if not USER_ID_RE.fullmatch((requester_id or "").strip()):
            raise InvalidRequest("요청자 정보가 없어 메일을 다룰 수 없습니다")
        return requester_id.strip()

    def _client(self, owner: str):
        try:
            return self.clients(owner)
        except AccountNotConnected as exc:
            raise InvalidRequest(NOT_CONNECTED) from exc

    async def _sync_before_search(self, owner: str) -> bool:
        """검색 전에 요청자 메일함의 새 메일을 짧게 가져온다. 실패해도 저장된 메일로 검색한다."""
        client = self._client(owner)
        try:
            connector = GmailSyncConnector(client, owner, max_message_bytes=self.settings.max_message_bytes)
            stats = await run_incremental(connector, self.store, max_pages=self.settings.search_sync_max_pages)
            return stats.completed
        except Exception as exc:  # 네트워크 오류 등 — 검색 자체는 계속
            log.warning("검색 전 동기화 실패 (%s): %s", owner, type(exc).__name__)
            return False

    async def search_mail(self, requester_id: str, query: str = "", max_results: int = 10) -> MailSearchResult:
        owner = self._owner(requester_id)
        query = query.strip()
        if len(query) > 200:
            raise InvalidRequest("검색어는 200자 이내로 입력하세요")
        limit = max(1, min(max_results, self.settings.max_search_results))
        synced = await self._sync_before_search(owner) if self.settings.sync_on_search else False

        hits = await self.store.search(owner, query, limit * 5)
        threads: dict[str, list] = {}
        for m in hits:  # 최신순 → 스레드별로 묶기
            threads.setdefault(m.thread_id, []).append(m)
        top = list(threads.items())[:limit]
        counts = await self.store.thread_counts(owner, [tid for tid, _ in top])
        summaries = [
            MailSummary(thread_id=tid, subject=next((m.subject for m in ms if m.subject), NO_SUBJECT),
                        sender=ms[0].sender, sent_at=ms[0].sent_at, message_count=counts.get(tid, len(ms)))
            for tid, ms in top
        ]
        return MailSearchResult(query=query, threads=summaries, synced=synced)

    async def read_mail(self, requester_id: str, thread_id: str) -> MailThread:
        owner = self._owner(requester_id)
        if not re.fullmatch(r"[0-9a-zA-Z_-]{1,64}", thread_id or ""):
            raise InvalidRequest(NOT_FOUND)
        messages = await self.store.thread(owner, thread_id)  # 주인 조건이 항상 붙는다
        if not messages:
            raise InvalidRequest(NOT_FOUND)
        truncated = any(m.body_omitted for m in messages)
        limit = self.settings.max_read_body_chars
        for m in messages:
            if len(m.body) > limit:
                m.body, truncated = m.body[:limit] + " …(이하 생략)", True
        return MailThread(thread_id=thread_id, subject=next((m.subject for m in messages if m.subject), NO_SUBJECT),
                          link=thread_link(thread_id), messages=messages, truncated=truncated)

    async def send_mail(
        self, requester_id: str, to: list[str], subject: str, body: str, cc: list[str] | None = None
    ) -> SendResult:
        owner = self._owner(requester_id)
        to = self._recipients(to, "받는 사람")
        cc = self._recipients(cc or [], "참조")
        if not to:
            raise InvalidRequest("받는 사람이 최소 1명 필요합니다")
        if len(to) + len(cc) > self.settings.max_recipients:
            raise InvalidRequest(f"받는 사람과 참조는 합쳐서 {self.settings.max_recipients}명까지입니다")
        subject = subject.strip()
        if not subject or len(subject) > self.settings.max_subject_chars or "\n" in subject or "\r" in subject:
            raise InvalidRequest(f"제목은 1~{self.settings.max_subject_chars}자 한 줄로 입력하세요")
        if not body.strip() or len(body) > self.settings.max_body_chars:
            raise InvalidRequest(f"본문은 1~{self.settings.max_body_chars}자로 입력하세요")
        client = self._client(owner)  # 요청자 본인 계정

        msg = EmailMessage()
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg["X-Agent-Requester"] = owner  # 에이전트를 통해 보낸 메일임을 메일 자체에도 남김
        msg.set_content(f"{body.rstrip()}\n\n--\n사내 AI 에이전트를 통해 발송된 메일입니다.")
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        sent = await client.send_message(raw)
        status = "simulated" if sent.get("simulated") else "sent"  # 가상 메일함이면 실제로 나가지 않았음을 표시
        return SendResult(message_id=sent["id"], thread_id=sent.get("threadId", ""), status=status, to=to, cc=cc)

    def _recipients(self, addresses: list[str], label: str) -> list[str]:
        out: list[str] = []
        for raw in addresses:
            addr = raw.strip().lower()
            if not EMAIL_RE.fullmatch(addr):
                raise InvalidRequest(f"{label} 이메일 형식이 올바르지 않습니다: {raw}")
            domains = self.settings.allowed_recipient_domains
            if domains and addr.rsplit("@", 1)[1] not in domains:
                raise InvalidRequest(f"허용되지 않은 수신 도메인입니다: {addr} (허용: {', '.join(domains)})")
            if addr not in out:
                out.append(addr)
        return out
