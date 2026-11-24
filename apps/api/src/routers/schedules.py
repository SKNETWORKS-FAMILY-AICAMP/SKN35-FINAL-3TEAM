"""예약 작업 (관리자). 목록·다음 실행 시각·마지막 결과를 보고, "지금 실행" 할 수 있다 (UC-02 "지금 만들기")."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from contracts.permission import Principal
from routers.deps import current_user

router = APIRouter(prefix="/api/admin/schedules", tags=["schedules"])


def _scheduler(request: Request, user: Principal = Depends(current_user)):
    if "admin" not in user.roles:
        raise HTTPException(403, "관리자만 예약 작업을 볼 수 있습니다.")
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(404, "예약 작업 설정(config/schedules.yaml)이 없습니다.")
    return scheduler


@router.get("")
async def list_jobs(scheduler=Depends(_scheduler)) -> dict:
    return {"running": scheduler._task is not None, "jobs": scheduler.status()}


@router.post("/{job_id}/run")
async def run_now(job_id: str, scheduler=Depends(_scheduler)) -> dict:
    if scheduler.get(job_id) is None:
        raise HTTPException(404, "없는 예약 작업입니다.")
    return await scheduler.run_job(job_id, trigger="manual")
