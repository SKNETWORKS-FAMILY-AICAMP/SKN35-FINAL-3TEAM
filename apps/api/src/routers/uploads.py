import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from contracts.permission import Principal
from routers.deps import current_user, get_services
from services.container import AppServices
from services.uploads import MAX_UPLOAD_BYTES

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


class UploadOut(BaseModel):
    file_id: str
    filename: str
    size_bytes: int


@router.post("", status_code=201, response_model=UploadOut)
async def upload_doc(
    file: UploadFile,
    user: Principal = Depends(current_user),
    svc: AppServices = Depends(get_services),
) -> UploadOut:
    try:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "파일은 20MB까지 첨부할 수 있습니다.")
        uploaded = await asyncio.to_thread(svc.uploads.create, user.user_id, file.filename or "", data)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except (OSError, sqlite3.Error) as error:
        raise HTTPException(503, "파일을 저장하지 못했습니다. 다시 시도해 주세요.") from error
    finally:
        await file.close()
    return UploadOut(**uploaded)
