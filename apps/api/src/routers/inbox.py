"""내 알림함 (UC-03). 본인 알림만 보이고, 다른 사람의 알림은 404."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from contracts.permission import Principal
from routers.deps import current_user, get_services
from services.container import AppServices

router = APIRouter(tags=["inbox"])


@router.get("/api/inbox")
async def list_inbox(user: Principal = Depends(current_user), svc: AppServices = Depends(get_services)) -> list[dict]:
    alerts = await asyncio.to_thread(svc.inbox.list, user.user_id)
    return [{key: alert[key] for key in ("alert_id", "kind", "title", "created_at", "read_at")}
            | {"counts": alert["payload"].get("counts", {})} for alert in alerts]


@router.get("/api/inbox/{alert_id}")
async def open_alert(alert_id: str, user: Principal = Depends(current_user),
                     svc: AppServices = Depends(get_services)) -> dict:
    alert = await asyncio.to_thread(svc.inbox.open, user.user_id, alert_id)
    if alert is None:
        raise HTTPException(404, "알림을 찾을 수 없습니다.")
    return alert
