"""직원 선택 → 기존 하네스 승인 → Gmail 발송. 발신자는 항상 로그인 사용자다."""

import asyncio
import hashlib
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from contracts.harness import LLMToolCall, Message
from contracts.permission import Principal, ToolCall
from harness import create_harness
from routers.chat import usable_agent
from routers.deps import current_user, get_services
from services.container import AppServices
from tools.gmail import GmailTool, SendMailInput

router = APIRouter(prefix="/api/mail", tags=["mail"])
log = logging.getLogger(__name__)


class SendDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    session_id: str = Field(min_length=1, max_length=100)
    request_id: uuid.UUID
    recipient_ids: list[str] = Field(min_length=1, max_length=10)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)

    @field_validator("subject")
    @classmethod
    def one_line(cls, value):
        if "\n" in value or "\r" in value:
            raise ValueError("제목은 한 줄로 입력해 주세요.")
        return value


async def mail_context(svc, user, session_id):
    session = await svc.sessions.get_owned(session_id, user.user_id)
    if session is None:
        raise HTTPException(404, "대화를 찾을 수 없습니다.")
    agent = await usable_agent(svc, user, session.agent_id, existing=True)
    tool = await svc.tools.get("send_mail")
    if not isinstance(tool, GmailTool) or (agent is not None and "send_mail" not in agent.tools):
        raise HTTPException(409, "이 에이전트에 메일 보내기 도구를 추가하고 새 대화에서 사용해 주세요.")
    return session, tool


async def recipient_directory(svc, user, tool):
    try:
        owner = await tool.connection.requester_id_for(user)
        if owner is None:
            raise ValueError("Gmail 연결 없음")
        candidates = {}
        for uid in svc.users.ids():
            if uid == user.user_id:
                continue
            person = svc.users.get(uid)
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
    except Exception as error:
        log.warning("Gmail recipient directory failed: %s", type(error).__name__, exc_info=True)
        raise HTTPException(503, "수신자 목록을 읽지 못했습니다. Gmail MCP 서버를 최신 코드로 다시 실행하고 본인 계정 연결을 확인해 주세요.") from error


@router.get("/recipients")
async def recipients(session_id: str, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services)):
    _, tool = await mail_context(svc, user, session_id)
    return await recipient_directory(svc, user, tool)


@router.post("/send")
async def send_draft(req: SendDraft, user: Principal = Depends(current_user), svc: AppServices = Depends(get_services)):
    session, tool = await mail_context(svc, user, req.session_id)
    fingerprint = hashlib.sha256(req.model_dump_json(exclude={"request_id"}).encode()).hexdigest()
    key = str(req.request_id)
    # 같은 요청 키는 수신자 연결 정보가 바뀌어도 재실행하지 않는다.
    try:
        reserved = await asyncio.to_thread(svc.mail_dispatches.reserve, user.user_id, key, fingerprint)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not reserved:
        previous = await asyncio.to_thread(svc.mail_dispatches.result, user.user_id, key)
        if previous is not None:
            return previous
        raise HTTPException(409, "발송 중이거나 결과를 확인하지 못한 요청입니다. 보낸편지함을 확인해 주세요.")
    try:
        directory = {item["user_id"]: item for item in await recipient_directory(svc, user, tool)}
        ids = list(dict.fromkeys(req.recipient_ids))
        if any(uid not in directory for uid in ids):
            raise HTTPException(422, "본인을 제외한 Gmail 연결 직원을 선택해 주세요.")
        arguments = SendMailInput(to=list(dict.fromkeys(directory[uid]["email"] for uid in ids)),
                                  subject=req.subject, body=req.body).model_dump()
    except HTTPException as error:
        await asyncio.to_thread(svc.mail_dispatches.finish, user.user_id, key,
                                {"status": "not_sent", "message": error.detail})
        raise
    run_id = str(uuid.uuid4())
    call = ToolCall(key, "send_mail", arguments, user, session.session_id, session.agent_id, run_id)
    runner = create_harness(llm=svc.llm, tools=svc.tools.only(["send_mail"]), checker=svc.checker,
                            approver=svc.approvals, audit=svc.audit, max_turns=1, tool_timeout=svc.tool_timeout)
    async with session.lock:
        outcome = await runner.execute_tool(call)
        payload = {}
        if outcome.ok:
            try:
                payload = json.loads(outcome.message.content)
            except ValueError:
                pass
        status = payload.get("status") if outcome.ok and isinstance(payload, dict) else None
        if status == "sent":
            text = "선택한 직원에게 메일을 발송했습니다."
        elif status == "simulated":
            text = "가상 메일 테스트가 완료되었습니다. 실제 메일은 발송되지 않았습니다."
        elif outcome.denial:
            status = "not_sent"
            text = {"rejected": "발송 승인을 거절하여 메일을 보내지 않았습니다.",
                    "timeout": "승인 대기 시간이 지나 메일을 보내지 않았습니다."}.get(
                        outcome.denial.kind, "메일을 발송하지 않았습니다. " + outcome.denial.reason)
        else:
            status, text = "unknown", "발송 성공을 확인하지 못했습니다. 자동 재시도하지 않았으니 보낸편지함을 확인해 주세요."
        result = {"status": status, "message": text}
        # 발송 결과를 먼저 영구 보관한다. 대화 저장 실패 뒤에도 같은 키로 재발송하지 않는다.
        await asyncio.to_thread(svc.mail_dispatches.finish, user.user_id, key, result)
        messages = [Message("user", "선택한 직원에게 메일 발송: " + req.subject),
                    Message("assistant", tool_calls=(LLMToolCall(key, "send_mail", arguments),)),
                    outcome.message, Message("assistant", text)]
        session.messages.extend(messages)
        await svc.sessions.record_turn(session, run_id, "denied" if outcome.denial else "completed", 0, messages, outcome.denial)
        return result
