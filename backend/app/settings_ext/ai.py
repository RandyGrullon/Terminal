"""Default settings of the local AI copilot (app.ai): a model served by Ollama, by default on this same machine. Off
until the user turns it on; web search only with the user's own key of an official search API."""
from __future__ import annotations

DEFAULTS: dict = {
    "ai.enabled": False,
    "ai.ollama_url": "http://127.0.0.1:11434",
    "ai.model": "",  # empty = the first model the Ollama server has
    "ai.search_provider": "none",  # none | brave (Brave Search API, with ai.brave_api_key)
    "ai.brave_api_key": "",  # a secret: masked in the terminal, never logged
    "ai.timeout_s": 120,  # seconds a local model may take to answer
}
