from fastapi import APIRouter
from pydantic import BaseModel, Field
from apps.api.src.services.chat import run_local_demo

router = APIRouter(prefix="/api/chat",tags=["chat"])

class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)

class ChatResponse(BaseModel):
    status: str
    message: str

@router.post("/messages", response_model=ChatResponse)
def send_message(request: ChatRequest) -> ChatResponse:
    result = run_local_demo(request.content)
    return ChatResponse(status=result.status, message=result.message)