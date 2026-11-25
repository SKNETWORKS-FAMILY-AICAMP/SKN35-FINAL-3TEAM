"""대화 세션 저장소 (COM-06).

저장소: 메모리(InMemorySessionStore, 기본·테스트) 또는 DB(services/db_stores.py 의 DbSessionStore, STORE=db).
세션마다 lock 을 둬서 같은 세션에 요청이 동시에 와도 기록이 섞이지 않게 한다.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from contracts.harness import Denial, Message, StopReason


@dataclass
class ChatSession:
    session_id: str
    user_id: str
    agent_id: str | None
    messages: list[Message] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    attachments: dict[str, dict] = field(default_factory=dict)
    # 권한 거부로 멈춘 요청: 몇 번째 질문(0부터)이 무엇 때문에 막혔는지. 다시 열 때 거부 안내를 그린다
    denials: dict[int, Denial] = field(default_factory=dict)


def turn_index(session: ChatSession) -> int:
    """방금 끝난 요청이 이 대화의 몇 번째 질문인지 (0부터). 하네스가 넣는 안내는 저장하지 않으므로 사용자 메시지 수로 센다."""
    return sum(1 for m in session.messages if m.role == "user") - 1


@dataclass(frozen=True)
class SessionSummary:
    """화면 왼쪽 '최근 대화' 한 줄. 메시지 본문은 열 때 따로 읽는다."""

    session_id: str
    agent_id: str | None
    title: str
    updated_at: datetime | None


def first_line(content: str, limit: int = 80) -> str:
    """대화 제목: 첫 사용자 메시지의 첫 줄."""
    return content.strip().splitlines()[0][:limit] if content.strip() else ""


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, ChatSession] = {}

    async def create(self, user_id: str, agent_id: str | None = None) -> ChatSession:
        s = ChatSession(str(uuid.uuid4()), user_id, agent_id)
        self._sessions[s.session_id] = s
        return s

    async def get_owned(self, session_id: str, user_id: str) -> ChatSession | None:
        """본인 세션만 돌려준다. 남의 세션이면 없는 것처럼 None."""
        s = self._sessions.get(session_id)
        return s if s is not None and s.user_id == user_id else None

    async def list_owned(self, user_id: str) -> list[ChatSession]:
        return [s for s in self._sessions.values() if s.user_id == user_id]

    async def delete_owned(self, session_id: str, user_id: str) -> bool:
        """본인 대화를 지운다. 남의 대화·없는 대화는 False (있는지도 알려주지 않음)."""
        s = self._sessions.get(session_id)
        if s is None or s.user_id != user_id:
            return False
        del self._sessions[session_id]
        return True

    async def summaries(self, user_id: str) -> list[SessionSummary]:
        """메시지가 있는 내 대화, 최근 것부터. 메모리 저장소는 시각을 따로 안 남겨서 만든 순서의 역순."""
        out = []
        for s in reversed(await self.list_owned(user_id)):
            first = next((m.content for m in s.messages if m.role == "user"), None)
            if first is not None:
                out.append(SessionSummary(s.session_id, s.agent_id, first_line(first), None))
        return out

    async def record_turn(self, session: ChatSession, run_id: str, stop_reason: StopReason, llm_calls: int,
                          new_messages: list[Message], denial: Denial | None = None) -> None:
        """요청 1건이 끝났을 때 새 메시지를 저장한다. 메모리 저장소는 session.messages 가 곧 기록이라 거부만 남긴다."""
        if denial is not None:
            session.denials[turn_index(session)] = denial
