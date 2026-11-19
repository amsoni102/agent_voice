"""Local LLM via the Ollama HTTP API already running on this machine."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator

import httpx

from .tools import TOOL_DEFINITIONS

log = logging.getLogger("agentvoice.ollama")

# Keep voice turns short and cheap on CPU. Large num_ctx/num_predict was 20–70s/turn.
VOICE_OPTIONS = {
    "temperature": 0.5,
    "top_p": 0.9,
    "num_ctx": 2048,
    "num_predict": 72,
}

# Slim schemas — full TOOL_DEFINITIONS bloat the prompt and slow llama3.2 a lot.
_VOICE_TOOL_NAMES = {
    "record_lead_info",
    "check_availability",
    "book_meeting",
    "schedule_callback",
    "set_outcome",
    "add_to_do_not_call",
    "transfer_to_human",
    "end_call",
}


_SCHEMA_KEYS = {"type", "function", "parameters", "name", "properties", "required", "description"}
_JSON_BLOB = re.compile(r"\{.*?\}|\[.*?\]", re.S)


def speakable(text: str) -> str:
    text = _JSON_BLOB.sub(" ", text or "")
    text = re.sub(r"^\s*(assistant|agent)\b\s*:?", " ", text, flags=re.I)
    text = re.sub(r"[*_#`<>{}\[\]|\"]", " ", text)
    text = re.sub(r"\b(?:end_call|book_meeting|check_availability|record_lead_info|set_outcome)\b\S*", " ", text)
    return " ".join(text.split()).strip(" ,;:")


def clean_tool_args(args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {}
    if args and set(args) <= _SCHEMA_KEYS:
        return {}
    return {k: v for k, v in args.items() if k not in _SCHEMA_KEYS and not isinstance(v, (dict, list))}


def _window(messages: list[dict[str, Any]], keep: int = 10) -> list[dict[str, Any]]:
    if messages and messages[0].get("role") == "system":
        return messages[:1] + messages[1:][-keep:]
    return messages[-keep:]


def chat_tools() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in TOOL_DEFINITIONS:
        if spec["name"] not in _VOICE_TOOL_NAMES:
            continue
        params = spec.get("parameters") or {"type": "object", "properties": {}}
        # Drop verbose property descriptions for speed; keep names + required.
        slim_props = {
            key: {"type": (val.get("type") if isinstance(val, dict) else "string")}
            for key, val in (params.get("properties") or {}).items()
        }
        for key, val in (params.get("properties") or {}).items():
            if isinstance(val, dict) and "enum" in val:
                slim_props[key]["enum"] = val["enum"]
        slim = {
            "type": "object",
            "properties": slim_props,
            "additionalProperties": False,
        }
        if params.get("required"):
            slim["required"] = params["required"]
        out.append(
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": (spec.get("description") or "")[:120],
                    "parameters": slim,
                },
            }
        )
    return out


class OllamaClient:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(timeout=120.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ping(self) -> str:
        resp = await self._client.get(f"{self.base_url}/api/tags")
        resp.raise_for_status()
        names = [m.get("name", "") for m in resp.json().get("models", [])]
        if self.model not in names and names:
            log.warning("Ollama model %s not in %s — Ollama will pull/load it on first use", self.model, names)
        return ", ".join(names) or "(no models)"

    async def warmup(self) -> None:
        try:
            await self._client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "ok"}],
                    "stream": False,
                    "keep_alive": "30m",
                    "options": {"num_predict": 1, "num_ctx": 512},
                },
            )
        except httpx.HTTPError as exc:
            log.warning("Ollama warmup failed: %s", exc)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: bool = True,
    ) -> tuple[str, list[dict[str, Any]]]:
        """One completion. Returns (assistant_text, tool_calls)."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": _window(messages),
            "stream": False,
            "keep_alive": "30m",
            "options": dict(VOICE_OPTIONS),
        }
        if tools:
            body["tools"] = chat_tools()
        resp = await self._client.post(f"{self.base_url}/api/chat", json=body)
        resp.raise_for_status()
        data = resp.json()
        message = data.get("message") or {}
        text = speakable(message.get("content") or "")
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
            calls.append({"name": fn.get("name", ""), "arguments": clean_tool_args(args)})
        log.info(
            "ollama chat model=%s eval=%s prompt_eval=%s tools=%s ms_tokens~%s",
            self.model,
            data.get("eval_count"),
            data.get("prompt_eval_count"),
            bool(tools),
            data.get("total_duration"),
        )
        return text, calls

    async def chat_stream(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        """Stream a tool-free reply as text pieces, so speech can start on the first sentence."""
        body = {
            "model": self.model,
            "messages": _window(messages),
            "stream": True,
            "keep_alive": "30m",
            "options": dict(VOICE_OPTIONS),
        }
        async with self._client.stream("POST", f"{self.base_url}/api/chat", json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                data = json.loads(line)
                piece = (data.get("message") or {}).get("content") or ""
                if piece:
                    yield piece
                if data.get("done"):
                    log.info(
                        "ollama stream model=%s eval=%s prompt_eval=%s total_ms=%s",
                        self.model, data.get("eval_count"), data.get("prompt_eval_count"),
                        int((data.get("total_duration") or 0) / 1e6),
                    )

    async def summarize_json(self, system: str, user: str) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system + "\nReply with JSON only."},
                {"role": "user", "content": user[-12_000:]},
            ],
            "stream": False,
            "format": "json",
            "keep_alive": "30m",
            "options": {"num_ctx": 4096, "num_predict": 400},
        }
        resp = await self._client.post(f"{self.base_url}/api/chat", json=body)
        resp.raise_for_status()
        content = (resp.json().get("message") or {}).get("content") or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {}
