"""에이전트 루프: LLM ↔ 툴 왕복을 max_turns 안에서 돌린다.

규칙은 contracts/harness.py 의 '에이전트 루프' 주석을 따른다.
"""

from __future__ import annotations

import asyncio
import logging
import json
import uuid
import re
from dataclasses import dataclass

from contracts.harness import (
    Denial,
    DenialKind,
    LLMClient,
    Message,
    RunResult,
    RequiredToolLLMClient,
    ToolRegistry,
    LLMToolCall,
)
from contracts.permission import (
    ApprovalHandler,
    ApprovalOutcome,
    AuditLogger,
    AuditRecord,
    Decision,
    PermissionChecker,
    Principal,
    ToolCall,
)
from harness.denial import DoneTool, template_reply
from harness.grounding import RequireSearch
from harness.review_grounding import LOOKUP_TOOL, SUBMISSION_TOOL, ReviewLookupGuard
from harness.attachment_grounding import compact_system, evidence_problem, extractive_reply, parse_record, read_call
from harness.market_grounding import market_plan

log = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT = 30.0  # HARNESS_TOOL_TIMEOUT_SECONDS (SYS-10)
_VERDICT: dict[str, str] = {  # LLM 에게 돌려주는 거부 메시지 머리말
    "deny": "permission denied",
    "rejected": "permission not approved (user rejected)",
    "timeout": "permission not approved (approval timed out)",
}
SKIPPED_CONTENT = "error: not executed: 앞선 도구 호출이 중단되어 실행하지 않았습니다"


@dataclass(frozen=True)
class _Outcome:
    message: Message  # role="tool"
    ok: bool = False  # 툴이 실행돼 성공했는가
    denial: Denial | None = None
    label: str = ""  # ToolSpec.label


class AgentRunner:
    def __init__(
        self,
        *,
        llm: LLMClient,
        tools: ToolRegistry,
        checker: PermissionChecker,
        approver: ApprovalHandler,
        audit: AuditLogger,
        max_turns: int,
        tool_timeout: float | None = DEFAULT_TOOL_TIMEOUT,
        require_search: RequireSearch | None = None,
        require_review_lookup: bool = False,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.checker = checker
        self.approver = approver
        self.audit = audit
        self.max_turns = max_turns
        self.tool_timeout = tool_timeout  # None 이면 제한 없음
        self.require_search = require_search  # 문서 질문이면 검색 강제 (harness/grounding.py)
        self.require_review_lookup = require_review_lookup

    async def run(
        self,
        user_input: str,
        principal: Principal,
        session_id: str,
        history: list[Message] | None = None,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
        attachments: list[dict] | None = None,
        attachment_role: str = "",
    ) -> RunResult:
        messages: list[Message] = list(history or [])
        messages.append(Message(role="user", content=user_input))
        specs = await self.tools.specs()
        if attachments and any(s.name == "parse_doc" for s in specs):
            return await self._read_attachments(messages, user_input, attachments, principal, session_id,
                                                agent_id, run_id, attachment_role)
        market = market_plan(user_input, specs)
        if market:
            return await self._run_market(messages, user_input, market, principal, session_id, agent_id, run_id)
        review = ReviewLookupGuard.from_request(user_input, history or []) if self.require_review_lookup else None
        review_spec = next((spec for spec in specs if spec.name == LOOKUP_TOOL), None)
        submission_spec = next((spec for spec in specs if spec.name == SUBMISSION_TOOL), None)
        if review and review_spec is None:
            final = Message("assistant", review.unavailable(tool_missing=True))
            messages.append(final)
            return RunResult(final, messages, 0, "completed")

        done: list[DoneTool] = []  # 이번 run 에서 성공한 툴 실행 (거부 시 마무리 답변에 씀)
        rs = self.require_search
        search_spec = next((s for s in specs if rs and s.name == rs.search_tool), None)
        grounded = False  # 이번 run 에서 문서 툴이 성공했는가
        force_search = False  # 다음 호출은 검색 강제
        forced = False  # run 당 한 번만
        retry_review = False
        retry_submission: tuple[str, ...] = ()

        for turn in range(1, self.max_turns + 1):
            if retry_submission:
                pending = retry_submission
                retry_submission = ()
                submission_messages = review.submission_messages(messages, pending)
                if isinstance(self.llm, RequiredToolLLMClient):
                    reply = (await self.llm.chat_required_tool(submission_messages, submission_spec)).message
                else:
                    reply = (await self.llm.chat(submission_messages, [submission_spec])).message
                calls = review.submission_calls(reply, pending)
                if not calls:
                    final = Message("assistant", review.summary())
                    messages.append(final)
                    return RunResult(final, messages, turn, "completed")
                reply = Message("assistant", "", tool_calls=calls)
            elif retry_review:
                retry_review = False
                reply = (await self.llm.chat([*messages, review.nudge()], [review_spec])).message
                calls = review.retry_calls(reply)
                if not calls:
                    final = Message("assistant", review.unavailable())
                    messages.append(final)
                    return RunResult(final, messages, turn, "completed")
                reply = Message("assistant", "", tool_calls=calls)
            elif force_search:
                force_search = False
                reply = rs.forced_call(await self._ask_search(messages, rs, search_spec), user_input)
            else:
                reply = (await self.llm.chat(list(messages), specs)).message

            if not reply.tool_calls:
                if review:
                    if review.missing:
                        if not review.retried and turn + 2 <= self.max_turns:
                            review.retried = retry_review = True
                            continue  # 확인하지 않은 모델 답변은 대화 기록에 넣지 않는다.
                        final = Message("assistant", review.unavailable())
                    else:
                        pending = review.pending_submissions()
                        if pending and submission_spec and turn + 2 <= self.max_turns:
                            review.submission_retry_requested = True
                            retry_submission = pending
                            continue
                        final = Message("assistant", review.summary())
                    messages.append(final)
                    return RunResult(final, messages, turn, "completed")
                # 문서 근거 없이 규정을 답했으면 그 답은 버리고 검색부터 시킨다 (강제 검색 + 답 쓰기 2회가 남아 있을 때만)
                if (search_spec and not grounded and not forced and turn + 2 <= self.max_turns
                        and rs.needed(user_input, reply.content, used_other_tools=bool(done))):
                    forced = force_search = True
                    log.info("문서 근거 없는 답이라 검색을 강제합니다 (run %s)", run_id)
                    continue
                messages.append(reply)
                return RunResult(reply, messages, turn, "completed")
            messages.append(reply)
            if turn == self.max_turns:
                break

            denial: Denial | None = None
            lookup_before_submission = False
            for llm_call in reply.tool_calls:
                if denial is not None:  # 규칙 8-b: 거부 뒤 남은 호출은 검사·실행하지 않는다
                    messages.append(Message("tool", SKIPPED_CONTENT, tool_call_id=llm_call.call_id))
                    continue
                call = ToolCall(
                    call_id=llm_call.call_id,
                    tool_name=llm_call.tool_name,
                    arguments=llm_call.arguments,
                    principal=principal,
                    session_id=session_id,
                    agent_id=agent_id,
                    run_id=run_id,
                )
                if review and call.tool_name == SUBMISSION_TOOL:
                    case_id = call.arguments.get("case_id")
                    if (not review.registration_requested or not isinstance(case_id, str)
                            or case_id not in review.records):
                        if review.registration_requested and isinstance(case_id, str) and case_id in review.case_ids:
                            lookup_before_submission = True
                        messages.append(Message("tool", "error: 심사 요청 등록은 명시적인 등록 요청과 해당 건의 조회 성공 후에만 진행할 수 있습니다.", tool_call_id=call.call_id))
                        continue
                    if case_id in review.submission_attempted:
                        messages.append(Message("tool", "error: 같은 심사 건의 등록을 이번 요청에서 이미 시도했습니다. 실행 결과를 확인해 주세요.", tool_call_id=call.call_id))
                        continue
                    review.submission_attempted.add(case_id)
                outcome = await self._handle_tool_call(call)
                messages.append(outcome.message)
                if outcome.ok:
                    if review:
                        review.record(call.tool_name, call.arguments, outcome.message.content)
                    done.append(DoneTool(outcome.label, outcome.message.content, call.tool_name))
                    grounded = grounded or bool(rs and call.tool_name in rs.grounded_tools)
                denial = outcome.denial

            if denial is not None:
                return await self._stop_denied(messages, denial, done, llm_calls=turn)
            if (review and review.missing and lookup_before_submission and not review.retried
                    and turn + 1 < self.max_turns):
                review.retried = retry_review = True
            if review and not review.missing and not review.registration_requested:
                # 단순 조회는 실제 결과로 바로 답해 불필요한 모델 호출과 재생성 오류를 줄인다.
                final = Message("assistant", review.summary())
                messages.append(final)
                return RunResult(final, messages, turn, "completed")
            if review and not review.missing:
                pending = review.pending_submissions()
                if pending and submission_spec and turn + 1 < self.max_turns:
                    review.submission_retry_requested = True
                    retry_submission = pending
                elif not pending or submission_spec is None or turn + 1 >= self.max_turns:
                    final = Message("assistant", review.summary())
                    messages.append(final)
                    return RunResult(final, messages, turn, "completed")

        return RunResult(None, messages, self.max_turns, "max_turns")

    async def _run_market(self, messages, question, plan, principal, session_id, agent_id, run_id):
        action, arguments, wants_report = plan
        done, calls_used = [], 0

        def finish(text):
            final = Message("assistant", text)
            messages.append(final)
            return RunResult(final, messages, calls_used, "completed")

        async def execute(name, args):
            ident = "market_" + uuid.uuid4().hex
            messages.append(Message("assistant", "", tool_calls=(LLMToolCall(ident, name, args),)))
            outcome = await self._handle_tool_call(ToolCall(ident, name, args, principal, session_id,
                                                            agent_id=agent_id, run_id=run_id))
            messages.append(outcome.message)
            if outcome.ok:
                done.append(DoneTool(outcome.label, outcome.message.content, name))
            return outcome

        if action == "clarify":
            return finish("조회할 기준일을 YYYY-MM-DD로 알려 주세요. 날짜를 추측해서 리포트를 만들지 않습니다.")
        selection = None
        if wants_report and await self.tools.get("search_report_templates") and await self.tools.get("get_report_template"):
            found = await execute("search_report_templates", {"query": question[:2000]})
            if found.denial:
                return await self._stop_denied(messages, found.denial, done, llm_calls=calls_used)
            if not found.ok:
                return finish("보고서 양식을 검색하지 못했습니다. " + found.message.content.removeprefix("error: "))
            candidates = json.loads(found.message.content)
            matches = candidates.get("matches", [])
            if not matches:
                return finish(candidates["markdown"] + "\n\n사용할 양식이 없어 보고서를 저장하지 않았습니다.")
            if len(matches) > 1 and matches[0]["score"] == matches[1]["score"]:
                return finish(candidates["markdown"] + "\n\n어느 양식을 사용할지 양식 이름을 알려 주세요.")
            chosen = matches[0]
            full = await execute("get_report_template", {"template_id": chosen["template_id"], "expected_hash": chosen["template_hash"]})
            if full.denial:
                return await self._stop_denied(messages, full.denial, done, llm_calls=calls_used)
            if not full.ok:
                return finish("선택한 보고서 양식을 읽지 못했습니다. " + full.message.content.removeprefix("error: "))
            selection = json.loads(full.message.content)
        outcome = await execute(action, arguments)
        if outcome.denial:
            return await self._stop_denied(messages, outcome.denial, done, llm_calls=calls_used)
        if not outcome.ok:
            return finish("시장 도구 실행을 완료하지 못했습니다.\n\n" + outcome.message.content.removeprefix("error: "))
        data = json.loads(outcome.message.content)
        if action in ("get_morning_report", "search_report_templates") or not wants_report:
            return finish(data.get("text", data.get("markdown", "")))
        if await self.tools.get("save_morning_report") is None:
            return finish(data["markdown"] + "\n\n이 에이전트에 리포트 저장 도구가 없어 조회 결과만 표시했습니다.")
        summary = ""
        if not re.search(r"표만|요약\s*없이", question) and self.max_turns > 0:
            try:
                body = data["markdown"]
                if selection:
                    template = selection["template"]
                    used = template["bindings"]
                    values = {key: data[key] for key in ("base_date", "mode")}
                    if "fx_table" in used or "data_quality" in used:
                        values["fx"] = data["fx"]
                    if "rates" in used or "data_quality" in used:
                        values["rates"] = data["rates"]
                    # 전체 양식은 서버가 보관/렌더링한다. 요약 모델에는 짧은 목차·작성 규칙만 줘 긴 양식도 자르지 않는다.
                    outline = ("\n".join(s["text"] for s in template["sections"] if s["type"] == "heading")
                               if "sections" in template else "\n".join(line for line in template["body_markdown"].splitlines() if line.startswith("#")))[:600]
                    body = ("사용할 양식:\n" + template["name"] + "\n" + template["description"][:400]
                            + "\n요약 작성 조건:\n" + template.get("summary_instructions", "")
                            + "\n양식 목차:\n" + outline + "\n확인한 수치:\n" + json.dumps(values, ensure_ascii=False))
                reply = (await self.llm.chat([
                    Message("system", "은행 직원용 시장 리포트의 요약만 한국어 3문장 이내로 작성한다. 선택한 양식의 목적과 항목 범위만 다루고 양식 전체를 다시 쓰지 않는다. 표의 수치와 단위를 그대로 쓰고 전망·원인 추측·투자 권유를 쓰지 않는다. 기록 재생이면 실시간이라고 하지 않는다. 실제 기준일이 요청일보다 과거이면 관측일 이후에도 변동이 없었다고 단정하지 않는다."),
                    Message("user", body),
                ], [])).message
                calls_used = 1
                summary = reply.content if len(reply.content) <= 2000 else ""
            except Exception:
                # 시장 수치/표는 모델 없이도 확인 가능하다. 연결 실패 시 지어낸 요약을 사용하지 않는다.
                calls_used = 1
                log.warning("시장 요약 생성 실패, 요약 없이 저장")
        save_args = {"snapshot_id": data["snapshot_id"], "summary": summary}
        if selection:
            save_args["template_selection_id"] = selection["template_selection_id"]
        outcome = await execute("save_morning_report", save_args)
        if outcome.denial:
            return await self._stop_denied(messages, outcome.denial, done, llm_calls=calls_used)
        if not outcome.ok:
            return finish(data["markdown"] + "\n\n리포트 저장 성공을 확인하지 못했습니다. " + outcome.message.content.removeprefix("error: "))
        saved = json.loads(outcome.message.content)
        return finish(saved.get("text", saved.get("markdown", "")))

    async def _read_attachments(self, messages, question, files, principal, session_id, agent_id, run_id, role):
        """첨부 질의는 서버가 파일·문단을 선택한다. 모든 읽기는 기존 권한/감사 경로를 지난다."""
        question = question.split("[사용자 첨부 파일 목록]", 1)[0]
        known = {f["file_id"]: f for f in files}
        records, done = [], []
        focus = [Message("system", compact_system(role)), Message("user", question)]

        async def execute(calls):
            assistant = Message("assistant", "", tool_calls=tuple(calls))
            messages.append(assistant)
            focus.append(assistant)
            for index, request in enumerate(calls):
                call = ToolCall(request.call_id, request.tool_name, request.arguments, principal, session_id,
                                agent_id=agent_id, run_id=run_id)
                outcome = await self._handle_tool_call(call)
                messages.append(outcome.message)
                focus.append(outcome.message)
                if outcome.denial:
                    for pending in calls[index + 1:]:
                        skipped = Message("tool", SKIPPED_CONTENT, tool_call_id=pending.call_id)
                        messages.append(skipped)
                        focus.append(skipped)
                    return outcome.denial
                if outcome.ok:
                    record = parse_record(outcome.message.content, known[call.arguments["file_id"]]["filename"])
                    if record:
                        records.append(record)
                    done.append(DoneTool(outcome.label, outcome.message.content, call.tool_name))
            return None

        denial = await execute([read_call(file, question) for file in files])
        if denial:
            return await self._stop_denied(messages, denial, done, llm_calls=0)
        if len(records) != len(files):
            final = Message("assistant", "첨부 문서를 읽는 과정에서 오류가 발생했습니다. 아래 도구 실행 결과를 확인해 주세요. 문서에 내용이 없다는 뜻은 아닙니다.")
            messages.append(final)
            return RunResult(final, messages, 0, "completed")
        if not any(r["text"].strip() for r in records):
            final = Message("assistant", extractive_reply(records))
            messages.append(final)
            return RunResult(final, messages, 0, "completed")

        # 서버가 관련 문단을 이미 읽었다. 답변 생성에는 불필요한 도구 스키마를 보내지 않아
        # 작은 컨텍스트에서도 원문이 잘리지 않게 한다. 잘못된 호출을 돌려주는 구현도 한 번만 처리한다.
        answer, calls_used, reread = "", 0, False
        for _ in range(min(self.max_turns, 2)):
            reply = (await self.llm.chat(focus, [])).message
            calls_used += 1
            if reply.tool_calls:
                reads = []
                if not reread:
                    for call in reply.tool_calls:
                        if (call.tool_name == "parse_doc" and isinstance(call.arguments.get("file_id"), str)
                                and call.arguments["file_id"] in known):
                            args = {**call.arguments, "max_chars": 1800}
                            args.setdefault("query", question[:2000])
                            reads.append(LLMToolCall(call.call_id, call.tool_name, args))
                if reads:
                    reread = True
                    denial = await execute(reads[:len(files)])
                    if denial:
                        return await self._stop_denied(messages, denial, done, llm_calls=calls_used)
                    continue
                focus.append(Message("system", "사내 검색이나 파일 ID 요청으로 바꾸지 말고, 이미 확인한 첨부 원문만으로 답하세요."))
                continue
            answer = reply.content
            issue = evidence_problem(answer, records, question)
            if issue is None:
                break
            log.info("첨부 답변 재검증: %s", issue)
            focus += [reply, Message("system", f"이 답은 사용할 수 없습니다: {issue} 확인한 text에서 직접 인용해 다시 답하세요.")]
        if not answer or evidence_problem(answer, records, question):
            answer = extractive_reply(records)
        final = Message("assistant", answer)
        messages.append(final)
        return RunResult(final, messages, calls_used, "completed")

    async def _ask_search(self, messages: list[Message], rs: RequireSearch, search_spec) -> Message | None:
        """검색 툴 하나만 주고 다시 묻는다. 안내 문구는 기록에 남기지 않는다. 실패하면 None (하네스가 직접 검색).

        LLM 이 특정 툴 호출 강제(tool_choice)를 지원하면 그걸 쓴다 (심사 등록 강제와 같은 방식).
        지원하지 않거나 그래도 안 부르면 RequireSearch.forced_call 이 질문에서 뽑은 검색어로 직접 부른다.
        """
        prompt = [*messages, rs.nudge()]
        try:
            if isinstance(self.llm, RequiredToolLLMClient):
                return (await self.llm.chat_required_tool(prompt, search_spec)).message
            return (await self.llm.chat(prompt, [search_spec])).message
        except Exception:
            log.warning("검색 강제 LLM 호출 실패, 질문으로 직접 검색", exc_info=True)
            return None

    async def _stop_denied(
        self, messages: list[Message], denial: Denial, done: list[DoneTool], llm_calls: int
    ) -> RunResult:
        """실행하지 않은 작업의 성공을 생성하지 않도록 서버가 최종 안내를 확정한다."""
        final = Message("assistant", template_reply(denial, done))
        messages.append(final)
        return RunResult(final, messages, llm_calls, "denied", denial)

    async def _handle_tool_call(self, call: ToolCall) -> _Outcome:
        def tool_msg(content: str) -> Message:
            return Message(role="tool", content=content, tool_call_id=call.call_id)

        tool = await self.tools.get(call.tool_name)
        if tool is None:
            return _Outcome(tool_msg(f"error: unknown tool '{call.tool_name}'"))

        result = await self.checker.check(call)
        spec = tool.spec

        approval: ApprovalOutcome | None = None
        if result.decision is Decision.ASK:
            approval = await self.approver.request_approval(call, result)
            allowed = approval == "approved"
        else:
            allowed = result.decision is Decision.ALLOW

        if not allowed:
            await self.audit.log(AuditRecord(call, result, approval=approval, executed=False))
            hook = getattr(tool, "on_not_approved", None)
            if approval is not None and hook is not None:  # 승인 카드에서 반려·시간 초과 (예: 고객 안내 반려 기록)
                try:
                    await hook(call, approval)
                except Exception:
                    log.warning("반려 처리 실패 (%s)", call.tool_name, exc_info=True)
            kind: DenialKind = "deny" if result.decision is Decision.DENY else (approval or "rejected")
            return _Outcome(
                tool_msg(f"error: {_VERDICT[kind]}: {result.reason}"),
                denial=Denial(call.call_id, call.tool_name, kind, result.reason, spec.display_name),
                label=spec.label,
            )

        ok = False
        # 툴이 자기 제한시간(timeout 속성)을 갖고 있으면 그걸 쓴다 (예: 문서 파싱은 오래 걸림). 없으면 하네스 기본값
        seconds = getattr(tool, "timeout", self.tool_timeout)
        limit = asyncio.timeout(seconds)
        try:
            async with limit:
                out = await tool.run(call)
            ok = out.ok
            content = out.content if out.ok else f"error: {out.content}"
        except TimeoutError as e:
            if limit.expired():  # 하네스 제한에 걸림. 툴이 스스로 던진 TimeoutError 와 구분한다
                content = f"error: tool '{call.tool_name}' timed out after {seconds:g}s"
            else:
                content = f"error: tool '{call.tool_name}' raised {type(e).__name__}: {e}"
        except Exception as e:  # 툴이 죽어도 루프는 계속 돈다 (CancelledError 는 BaseException 이라 그대로 전파)
            content = f"error: tool '{call.tool_name}' raised {type(e).__name__}: {e}"

        await self.audit.log(AuditRecord(call, result, approval=approval, executed=True, tool_ok=ok))
        return _Outcome(tool_msg(content or "(empty result)"), ok=ok, label=spec.label)


def create_harness(
    *,
    llm: LLMClient,
    tools: ToolRegistry,
    checker: PermissionChecker,
    approver: ApprovalHandler,
    audit: AuditLogger,
    max_turns: int,
    tool_timeout: float | None = DEFAULT_TOOL_TIMEOUT,
    require_search: RequireSearch | None = None,
    require_review_lookup: bool = False,
) -> AgentRunner:
    return AgentRunner(
        llm=llm, tools=tools, checker=checker,
        approver=approver, audit=audit, max_turns=max_turns,
        tool_timeout=tool_timeout, require_search=require_search, require_review_lookup=require_review_lookup,
    )
