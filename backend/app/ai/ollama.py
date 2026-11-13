"""Client of an Ollama server (https://github.com/ollama/ollama, REST API): the models it has (GET /api/tags) and a chat
turn with tools (POST /api/chat, not streamed). Plain requests through app.util.http_client (CLAUDE.md rule 4); the
URL is the user's own setting (ai.ollama_url, by default this machine)."""
from __future__ import annotations

import re
from typing import Any

import httpx

from app.util import http_client

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class OllamaError(Exception):
    """Ollama could not be reached or refused the request (Spanish message for the user)."""


def clean_reply(text: Any) -> str:
    """The model's answer without its <think>…</think> reasoning blocks (some models print them)."""
    return THINK_RE.sub("", str(text or "")).strip()


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self.base = str(base_url or "").strip().rstrip("/")
        self.timeout = float(timeout)

    def _url(self, path: str) -> str:
        if not re.match(r"^https?://[^\s/]+", self.base):
            raise OllamaError(f"La dirección de Ollama no es válida: «{self.base}» (ejemplo: http://127.0.0.1:11434).")
        return f"{self.base}{path}"

    async def models(self) -> list[str]:
        """Names of the models the server has (e.g. "qwen3:8b"), in its order."""
        url = self._url("/api/tags")
        try:
            async with http_client(timeout=10.0) as client:
                r = await client.get(url)
        except httpx.HTTPError as exc:
            raise OllamaError(f"No se pudo conectar con Ollama en {self.base}: ¿está instalado y en marcha? "
                              f"({type(exc).__name__})") from None
        if r.status_code >= 400:
            raise OllamaError(f"Ollama respondió {r.status_code} al pedir sus modelos.")
        try:
            data = r.json()
        except ValueError:
            raise OllamaError("Ollama respondió algo que no es JSON al pedir sus modelos.") from None
        return [str(m["name"]) for m in (data.get("models") or []) if isinstance(m, dict) and m.get("name")]

    async def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None, *,
                   num_ctx: int | None = None) -> dict:
        """One chat turn: {"message": {"role", "content", "tool_calls"?}, ...}. num_ctx: the context window Ollama
        reserves (its own default, often 4096 tokens, cuts long conversations with tools silently)."""
        options: dict[str, Any] = {"temperature": 0.2}
        if num_ctx:
            options["num_ctx"] = int(num_ctx)
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False, "options": options}
        if tools:
            body["tools"] = tools
        url = self._url("/api/chat")
        try:
            async with http_client(timeout=self.timeout) as client:
                r = await client.post(url, json=body)
        except httpx.TimeoutException:
            raise OllamaError(f"El modelo {model} tardó más de {int(self.timeout)} s en responder (puedes subir el "
                              "límite en Ajustes → Copiloto IA o usar un modelo más pequeño).") from None
        except httpx.HTTPError as exc:
            raise OllamaError(f"No se pudo conectar con Ollama en {self.base} ({type(exc).__name__}).") from None
        if r.status_code >= 400:
            detail = ""
            try:
                detail = str((r.json() or {}).get("error") or "")[:200]
            except ValueError:
                pass
            raise OllamaError(f"Ollama respondió {r.status_code}" + (f": {detail}" if detail else "") + ".")
        try:
            data = r.json()
        except ValueError:
            raise OllamaError("Ollama respondió algo que no es JSON.") from None
        if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
            raise OllamaError("Respuesta de Ollama sin mensaje.")
        return data
