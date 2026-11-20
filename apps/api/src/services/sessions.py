"""대화 세션 저장소 (COM-06).

지금은 메모리에 둔다. DB 가 붙으면 chat_session / message 테이블 구현으로 바꾼다.
세션마다 lock 을 둬서 같은 세션에 요청이 동시에 와도 기록이 섞이지 않게 한다.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from contracts.harness import Message


@dataclass
class ChatSession:
    session_id: str
    user_id: str
    agent_id: str | None
    messages: list[Message] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, ChatSession] = {}

    def create(self, user_id: str, agent_id: str | None = None) -> ChatSession:
        s = ChatSession(str(uuid.uuid4()), user_id, agent_id)
        self._sessions[s.session_id] = s
        return s

    def get_owned(self, session_id: str, user_id: str) -> ChatSession | None:
        """본인 세션만 돌려준다. 남의 세션이면 없는 것처럼 None."""
        s = self._sessions.get(session_id)
        return s if s is not None and s.user_id == user_id else None

    def list_owned(self, user_id: str) -> list[ChatSession]:
        return [s for s in self._sessions.values() if s.user_id == user_id]
