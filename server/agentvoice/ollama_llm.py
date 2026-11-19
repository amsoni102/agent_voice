"""Local LLM via the Ollama HTTP API already running on this machine."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from .tools import TOOL_DEFINITIONS

log = logging.getLogger("agentvoice.ollama")


def chat_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec.get("description", ""),
                "parameters": spec.get("parameters") or {"type": "object", "properties": {}},
            },
        }
        for spec in TOOL_DEFINITIONS
    ]


class OllamaClient:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def ping(self) -> str:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
            names = [m.get("name", "") for m in resp.json().get("models", [])]
            if self.model not in names and names:
                log.warning("Ollama model %s not in %s — Ollama will pull/load it on first use", self.model, names)
            return ", ".join(names) or "(no models)"

    async def warmup(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                await client.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": "ok"}],
                        "stream": False,
                        "keep_alive": -1,
                        "options": {"num_predict": 1},
                    },
                )
        except httpx.HTTPError as exc:
            log.warning("Ollama warmup failed: %s", exc)

    async def chat(self, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """One completion. Returns (assistant_text, tool_calls)."""
        body = {
            "model": self.model,
            "messages": messages,
            "tools": chat_tools(),
            "stream": False,
            "keep_alive": -1,
            "options": {"temperature": 0.6, "num_ctx": 8192, "num_predict": 280},
        }
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=body)
            resp.raise_for_status()
            data = resp.json()
        message = data.get("message") or {}
        text = (message.get("content") or "").strip()
        raw_calls = message.get("tool_calls") or []
        calls = []
        for item in raw_calls:
            fn = item.get("function") or item
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    args = {}
            calls.append({"name": fn.get("name", ""), "arguments": args})
        return text, calls

    async def summarize_json(self, system: str, user: str) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system + "\nReply with JSON only."},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "keep_alive": -1,
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=body)
            resp.raise_for_status()
            content = (resp.json().get("message") or {}).get("content") or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {}
