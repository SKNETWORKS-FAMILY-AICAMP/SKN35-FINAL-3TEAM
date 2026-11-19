"""메일 툴의 업무 로직 — 권한 확인 · 입력 검증. MCP와 무관하게 단위 테스트한다.

권한 원칙 (요구사항 SYS-03 "권한 필터는 툴 내부에서 강제, LLM이 고르지 않음"과 같은 방식)
- requester_email 은 LLM이 아니라 플랫폼이 로그인 사용자 이메일로 채운다 (manifest 의 inject)
- 팀 공용 메일함이지만, 요청자가 **참여자(From·To·Cc·Bcc)인 메일만** 검색·열람할 수 있다
- Gmail 검색어에 참여자 조건을 붙이고, 결과를 받은 뒤 다시 한 번 참여자 여부를 확인한다 (이중 확인)
"""

from __future__ import annotations

import base64
import re
from email.message import EmailMessage

from .client import GmailClient
from .config import Settings
from .convert import build_access, message_from_api, thread_link
from .models import MailSearchResult, MailSummary, MailThread, SendResult

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
NOT_FOUND = "메일을 찾을 수 없거나 열람 권한이 없습니다"  # 존재 여부를 드러내지 않도록 같은 문구 사용


class InvalidRequest(ValueError):
    """사용자(에이전트)에게 그대로 보여줘도 되는 입력 오류."""


class MailService:
    def __init__(self, client: GmailClient, settings: Settings | None = None) -> None:
        self.client = client
        self.settings = settings or Settings()
        self._owner: str | None = None

    async def owner(self) -> str:
        if self._owner is None:
            self._owner = (await self.client.get_profile_email()).lower()
        return self._owner

    @staticmethod
    def _requester(email: str) -> str:
        email = (email or "").strip().lower()
        if not EMAIL_RE.fullmatch(email):
            raise InvalidRequest("요청자 이메일 정보가 없어 메일을 다룰 수 없습니다")
        return email

    async def search_mail(self, requester_email: str, query: str = "", max_results: int = 10) -> MailSearchResult:
        me = self._requester(requester_email)
        query = query.strip()
        if len(query) > 200:
            raise InvalidRequest("검색어는 200자 이내로 입력하세요")
        limit = max(1, min(max_results, self.settings.max_search_results))
        # 사용자 검색어의 중괄호는 제거해 참여자 조건 그룹을 깨지 못하게 한다
        safe = query.replace("{", " ").replace("}", " ")
        gmail_query = f"{{from:{me} to:{me} cc:{me} bcc:{me}}}" + (f" ({safe})" if safe else "")

        thread_ids, _ = await self.client.list_threads(gmail_query, None, limit)
        owner = await self.owner()
        summaries = []
        for thread_id in thread_ids:
            thread = await self.client.get_thread(thread_id, metadata_only=True)
            if not thread or not thread.get("messages"):
                continue
            messages = [message_from_api(m, with_body=False) for m in thread["messages"]]
            if not build_access(messages, owner).allows(me):  # 이중 확인
                continue
            last = messages[-1]
            summaries.append(MailSummary(
                thread_id=thread_id,
                subject=next((m.subject for m in messages if m.subject), "(제목 없음)"),
                sender=last.sender, sent_at=last.sent_at, message_count=len(messages),
            ))
        return MailSearchResult(query=query, threads=summaries)

    async def read_mail(self, requester_email: str, thread_id: str) -> MailThread:
        me = self._requester(requester_email)
        if not re.fullmatch(r"[0-9a-zA-Z_-]{1,64}", thread_id or ""):
            raise InvalidRequest(NOT_FOUND)
        thread = await self.client.get_thread(thread_id)
        if not thread or not thread.get("messages"):
            raise InvalidRequest(NOT_FOUND)
        messages = [message_from_api(m) for m in thread["messages"]]
        if not build_access(messages, await self.owner()).allows(me):
            raise InvalidRequest(NOT_FOUND)

        truncated = False
        limit = self.settings.max_read_body_chars
        for m in messages:
            if len(m.body) > limit:
                m.body, truncated = m.body[:limit] + " …(이하 생략)", True
        return MailThread(
            thread_id=thread_id,
            subject=next((m.subject for m in messages if m.subject), "(제목 없음)"),
            link=thread_link(thread_id), messages=messages, truncated=truncated,
        )

    async def send_mail(
        self, requester_id: str, requester_email: str, to: list[str], subject: str, body: str, cc: list[str] | None = None
    ) -> SendResult:
        me = self._requester(requester_email)
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

        msg = EmailMessage()
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg["Reply-To"] = me  # 답장은 요청한 직원에게
        msg["X-Agent-Requester"] = requester_id  # 누가 시킨 발송인지 메일 자체에도 남김
        msg.set_content(f"{body.rstrip()}\n\n--\n{me} 님의 요청으로 사내 AI 에이전트가 발송한 메일입니다.")
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        sent = await self.client.send_message(raw)
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
