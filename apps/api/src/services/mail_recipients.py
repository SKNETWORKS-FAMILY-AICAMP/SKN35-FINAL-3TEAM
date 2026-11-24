"""메일 받는 사람 목록: 본인을 뺀, Gmail 계정을 연결한 직원 (이름·이메일).

화면의 '메일로 보내기'(routers/mail.py)와 플래너의 메일 보내기 단계(harness/steps.py)가 같이 쓴다.
받는 사람 주소는 언제나 이 목록(서버 확인)에서만 고른다. LLM 이 주소를 정하지 않는다.
"""

from __future__ import annotations

import re

from contracts.permission import Principal


async def mail_recipients(users, user: Principal, tool) -> list[dict]:
    """[{"user_id", "name", "email"}]. 연결이 없거나 MCP 조회가 실패하면 예외 (부른 쪽이 안내 문구로 바꾼다)."""
    owner = await tool.connection.requester_id_for(user)
    if owner is None:
        raise ValueError("Gmail 연결 없음")
    candidates = {}
    for uid in users.ids():
        if uid == user.user_id:
            continue
        person = users.get(uid)
        mailbox = await tool.connection.requester_id_for(person)
        if mailbox and mailbox != owner:
            candidates[uid] = (person, mailbox)
    async with tool.connection.session() as client:
        response = await client.call_tool("list_mail_recipients", {
            "requester_id": owner, "candidate_ids": [item[1] for item in candidates.values()],
        })
    if response.is_error or not isinstance(response.structured_content, dict):
        raise ValueError("수신자 목록 조회 실패")
    emails = {item["user_id"]: item["email"] for item in response.structured_content["recipients"]}
    return [{"user_id": uid, "name": person.attributes.get("name", uid), "email": emails[mailbox]}
            for uid, (person, mailbox) in candidates.items() if mailbox in emails]


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def named_recipients(question: str, directory: list[dict]) -> tuple[list[dict], list[str]]:
    """요청에서 받는 사람을 찾는다: 목록의 이름·사번·이메일이 요청에 적혀 있는 직원.
    돌려주는 값: (찾은 직원, 같은 이름이 여러 명이라 고를 수 없는 이름). 목록에 없는 주소는 받지 않는다."""
    found, ambiguous = [], []
    by_name: dict[str, list[dict]] = {}
    for person in directory:
        by_name.setdefault(person["name"], []).append(person)
    typed = {address.casefold() for address in _EMAIL.findall(question)}
    for person in directory:
        if person["email"].casefold() in typed or re.search(rf"(?<![\w]){re.escape(person['user_id'])}(?![\w])", question):
            found.append(person)
    for name, people in by_name.items():
        if len(name) >= 2 and name in question:
            if len(people) == 1:
                found.append(people[0])
            else:
                ambiguous.append(name)
    unique = list({p["user_id"]: p for p in found}.values())
    return unique, ambiguous
