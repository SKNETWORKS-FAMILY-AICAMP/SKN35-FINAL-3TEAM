"""내 문서와 내려받기. 모든 서류(시장 리포트·정리표·점검표 …)가 같은 API 로 PDF·DOCX 를 받는다.

저장된 문서를 id 로 찾아 서버가 변환한다. 브라우저가 보낸 내용을 변환하지 않는다 (저장된 적 없는 문서를 만들 수 없게).
본인 문서만, 다른 사람 것은 404.
"""

from __future__ import annotations

import asyncio
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response

from contracts.permission import Principal
from routers.deps import current_user, get_services
from services.container import AppServices
from services.document_render import FORMATS, FontNotFound, InvalidDocument, file_name, render

router = APIRouter(tags=["documents"])


@router.get("/api/documents")
async def list_documents(user: Principal = Depends(current_user), svc: AppServices = Depends(get_services)) -> list[dict]:
    return await svc.documents.list(user.user_id)


async def _own(svc: AppServices, user: Principal, document_id: str) -> dict:
    document = await svc.documents.get(user.user_id, document_id)
    if document is None:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    return document


@router.get("/api/documents/{document_id}")
async def get_document(document_id: str, user: Principal = Depends(current_user),
                       svc: AppServices = Depends(get_services)) -> dict:
    return await _own(svc, user, document_id)


@router.delete("/api/documents/{document_id}", status_code=204)
async def delete_document(document_id: str, user: Principal = Depends(current_user),
                          svc: AppServices = Depends(get_services)) -> Response:
    """내 문서함에서 지운다. 본인 문서만, 다른 사람 것·없는 것은 404."""
    if not await svc.documents.delete(user.user_id, document_id):
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    return Response(status_code=204)


@router.get("/api/documents/{document_id}/download")
async def download(document_id: str, format: Literal["pdf", "docx"] = "pdf",
                   user: Principal = Depends(current_user), svc: AppServices = Depends(get_services)) -> Response:
    record = await _own(svc, user, document_id)
    document = record.get("document")
    if document is None:
        raise HTTPException(409, "이전 형식(Markdown) 보고서는 파일로 내려받을 수 없습니다. 보고서를 다시 만들어 주세요.")
    try:
        content = await asyncio.to_thread(render, document, format)
    except FontNotFound as error:
        raise HTTPException(500, str(error)) from error
    except InvalidDocument as error:
        raise HTTPException(409, f"문서 형식이 올바르지 않아 내려받을 수 없습니다: {error}") from error
    await svc.documents.log_download(user.user_id, document_id, format)
    name = file_name(document, format)
    return Response(content, media_type=FORMATS[format], headers={
        "Content-Disposition": f"attachment; filename=\"document.{format}\"; filename*=UTF-8''{quote(name)}",
        "Cache-Control": "no-store",  # 고객 정보가 든 문서는 브라우저 캐시에 남기지 않는다
    })
