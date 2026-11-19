from fastapi import FastAPI
from apps.api.src.routers.chat import router as chat_router

app = FastAPI(title="Team Agent API")
app.include_router(chat_router)

@app.get("/health")
def health():
    return {"status" : "ok"}