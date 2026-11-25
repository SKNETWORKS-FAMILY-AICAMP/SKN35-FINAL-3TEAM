"""로그인 구현이 제공할 사용자별 Gmail 연결 정보 계약.

resolver는 인증된 플랫폼 user_id로 서버 저장소를 조회한다.
mailbox_user_id는 Gmail MCP의 계정/토큰 소유자 키이며 이메일 주소가 아니다.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class GmailAccountLink:
    platform_user_id: str
    mailbox_user_id: str


# 계정 미연결/연결 해제는 None. 조회 실패는 예외로 알린다.
GmailAccountResolver = Callable[[str], Awaitable[GmailAccountLink | None]]
