"""Copilot API (app.ai): GET /api/ai/status -> AIStatus · POST /api/ai/chat {messages} -> AIChatReply.

400 {detail} in Spanish when the copilot is off or the question is missing; 503 when Ollama cannot be reached or
refuses the request."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.runtime import rt
from app.util import clean_json

router = APIRouter()


class ChatMessage(BaseModel):
    role: str = Field(..., description="user | assistant")
    content: str = Field(..., max_length=20000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., max_length=50, description="La conversación, la pregunta al final")


def _service():
    svc = getattr(rt, "ai", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="El copiloto no está cargado")
    return svc


@router.get("/api/ai/status")
async def ai_status() -> dict:
    """AIStatus: settings (never the search key), whether Ollama answers and its models."""
    return clean_json(await _service().check())


@router.post("/api/ai/chat")
async def ai_chat(req: ChatRequest) -> dict:
    """AIChatReply: the copilot's answer and the read-only tools it used."""
    from app.ai.ollama import OllamaError
    from app.ai.service import AIError

    svc = _service()
    try:
        return clean_json(await svc.chat([m.model_dump() for m in req.messages]))
    except AIError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except OllamaError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
