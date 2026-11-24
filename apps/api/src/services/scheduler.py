"""예약 실행 (유즈케이스의 시간 액터). config/schedules.yaml 의 작업을 평일 정해진 시각에 돌린다.

- market_collect  09:10  UC-02-1 시장 데이터 수집·적재. 시장 MCP 의 collect_market_data 를 부른다
                         (ECOS 인증키·시장 DB 연결은 MCP 프로세스에만)
- market_report   09:20  UC-02 금리·환율 리포트. 직원마다 에이전트 하네스를 그 직원 권한으로 돌린다
                         (시장 툴만 준 하네스 → 코드가 순서를 정하는 흐름, 채팅에서 만든 것과 같은 리포트)
- maturity_alert  09:30  UC-03 대출 만기 알림. 담당자마다 send_maturity_alert 를 그 담당자 권한으로 부른다
                         (안내 문장 없이 목록만. 같은 기준일·기간 알림은 다시 만들지 않는다)

툴 호출은 모두 권한 검사(ALLOW 일 때만)와 감사 기록을 거친다. 승인(ASK)이 필요한 툴은 예약 실행에서 부르지 않는다.
SCHEDULER_ENABLED=true 일 때만 시각에 맞춰 돈다. 관리자는 언제든 run_job 으로 바로 돌릴 수 있다 (routers/schedules.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from contracts.permission import AuditRecord, Decision, Principal, ToolCall

log = logging.getLogger("uvicorn.error")

MARKET_REQUEST = "오늘 아침 금리·환율 리포트 만들어 줘"
MARKET_TOOLS = ("search_report_templates", "get_report_template", "market_snapshot", "save_morning_report")


@dataclass
class Job:
    id: str
    name: str
    at: time
    options: dict[str, Any] = field(default_factory=dict)
    last_run: dict | None = None


def load_jobs(path: Path) -> tuple[ZoneInfo, list[Job]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    jobs = []
    for item in raw.get("jobs", []):
        hour, minute = (int(part) for part in str(item["at"]).split(":"))
        options = {k: v for k, v in item.items() if k not in ("id", "name", "at")}
        jobs.append(Job(item["id"], item.get("name", item["id"]), time(hour, minute), options))
    return ZoneInfo(raw.get("timezone", "Asia/Seoul")), jobs


def next_run(now: datetime, at: time) -> datetime:
    """now 뒤의 첫 평일 at 시각."""
    candidate = datetime.combine(now.date(), at, tzinfo=now.tzinfo)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # 토·일 건너뜀
        candidate += timedelta(days=1)
    return candidate


class Scheduler:
    def __init__(self, svc, path: Path) -> None:
        self.svc = svc
        self.tz, self.jobs = load_jobs(path)
        self._task: asyncio.Task | None = None
        self._locks: dict[str, asyncio.Lock] = {job.id: asyncio.Lock() for job in self.jobs}

    def status(self) -> list[dict]:
        now = datetime.now(self.tz)
        return [{"id": job.id, "name": job.name, "at": job.at.strftime("%H:%M"),
                 "next_run": next_run(now, job.at).isoformat(), "last_run": job.last_run} for job in self.jobs]

    def get(self, job_id: str) -> Job | None:
        return next((job for job in self.jobs if job.id == job_id), None)

    async def run_job(self, job_id: str, *, trigger: str = "manual") -> dict:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        async with self._locks[job.id]:  # 같은 작업이 겹쳐 돌지 않게
            started = datetime.now(self.tz)
            try:
                result = await getattr(self, "_" + job.id)(job)
                status = "ok"
            except Exception as error:  # 작업 하나가 죽어도 예약 실행은 계속된다
                log.warning("예약 작업 실패 (%s)", job.id, exc_info=True)
                result, status = {"error": f"{type(error).__name__}: {error}"[:300]}, "error"
            job.last_run = {"trigger": trigger, "started_at": started.isoformat(), "status": status, "result": result}
            log.info("예약 작업 %s (%s): %s", job.id, trigger, status)
            return job.last_run

    # ── 작업 ──────────────────────────────────────────

    async def _market_collect(self, job: Job) -> dict:
        """시장 MCP 의 collect_market_data 를 부른다. ECOS 인증키·시장 DB 연결은 MCP 프로세스에만 있다."""
        tool = await self.svc.tools.get("market_snapshot")
        connection = getattr(tool, "connection", None)
        if connection is None:
            return {"skipped": "시장 MCP(MARKET_MCP_URL)가 연결되어 있지 않습니다."}
        async with connection.session() as client:
            result = await client.call_tool("collect_market_data", {"days": int(job.options.get("days", 10))})
        text = "\n".join(b.text for b in result.content if getattr(b, "type", "") == "text")
        if result.is_error:
            raise RuntimeError(text or "시장 MCP 가 수집을 거부했습니다.")
        if isinstance(result.structured_content, dict) and "status" in result.structured_content:
            return result.structured_content
        try:
            return json.loads(text)
        except ValueError:
            return {"result": text[:500]}

    async def _market_report(self, job: Job) -> dict:
        from harness import create_harness

        names = [name for name in MARKET_TOOLS if await self.svc.tools.get(name) is not None]
        if "market_snapshot" not in names or "save_morning_report" not in names:
            return {"skipped": "시장 도구(market_snapshot·save_morning_report)가 등록되어 있지 않습니다."}
        tools = self.svc.tools.only(names)
        results = {}
        for user_id in job.options.get("users", []):
            principal = self._principal(user_id)
            if principal is None:
                results[user_id] = "없는 사용자"
                continue
            harness = create_harness(llm=self.svc.llm, tools=tools, checker=self.svc.checker, approver=_NoApproval(),
                                     audit=self.svc.audit, max_turns=self.svc.max_turns, tool_timeout=self.svc.tool_timeout)
            run = await harness.run(MARKET_REQUEST, principal, f"schedule-{job.id}", run_id=str(uuid.uuid4()))
            saved = [m for m in run.messages if m.role == "tool" and '"report_id"' in m.content]
            results[user_id] = "저장" if saved else (run.final_message.content[:200] if run.final_message else run.stop_reason)
        return {"reports": results}

    async def _maturity_alert(self, job: Job) -> dict:
        tool = await self.svc.tools.get("send_maturity_alert")
        if tool is None:
            return {"skipped": "send_maturity_alert 도구가 등록되어 있지 않습니다."}
        results = {}
        for officer in await asyncio.to_thread(tool.loans.officers):
            principal = self._principal(officer)
            if principal is None:
                results[officer] = "없는 사용자"
                continue
            out = await self._call(tool, principal, {"days": int(job.options.get("days", 30))}, job.id)
            data = out.get("data") or {}
            results[officer] = ("보냄" if data.get("sent") else "이미 보냄" if data.get("already_sent")
                                else data.get("message") or out.get("error", "실패"))
        return {"alerts": results}

    # ── 도우미 ────────────────────────────────────────

    def _principal(self, user_id: str) -> Principal | None:
        return self.svc.users.get(user_id)

    async def _call(self, tool, principal: Principal, arguments: dict, job_id: str) -> dict:
        """권한 검사 → (ALLOW 일 때만) 실행 → 감사 기록. 채팅 하네스와 같은 순서 (SYS-07)."""
        call = ToolCall(f"schedule_{uuid.uuid4().hex[:12]}", tool.spec.name, arguments, principal,
                        f"schedule-{job_id}", None, None)
        verdict = await self.svc.checker.check(call)
        if verdict.decision is not Decision.ALLOW:
            await self.svc.audit.log(AuditRecord(call, verdict, executed=False))
            return {"error": f"권한 판정 {verdict.decision.value}: {verdict.reason}"}
        out = await tool.run(call)
        await self.svc.audit.log(AuditRecord(call, verdict, executed=True, tool_ok=out.ok))
        return {"ok": out.ok, "data": out.data, "error": None if out.ok else out.content}

    # ── 시각에 맞춰 돌기 ───────────────────────────────

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="scheduler")
            log.info("예약 실행 시작: %s", ", ".join(f"{j.id} {j.at:%H:%M}" for j in self.jobs))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self.jobs:
            now = datetime.now(self.tz)
            wake, job = min(((next_run(now, job.at), job) for job in self.jobs), key=lambda pair: pair[0])
            await asyncio.sleep(max(0.0, (wake - datetime.now(self.tz)).total_seconds()))
            await self.run_job(job.id, trigger="schedule")
            await asyncio.sleep(1)  # 같은 시각을 두 번 잡지 않게


class _NoApproval:
    """예약 실행에는 승인할 사람이 없다. 승인이 필요한 툴은 거절로 처리한다."""

    async def request_approval(self, call, result):
        return "rejected"


def scheduler_from_env(svc) -> Scheduler | None:
    path = Path(os.environ.get("SCHEDULES_PATH", "config/schedules.yaml"))
    if not path.is_file():
        return None
    return Scheduler(svc, path)
