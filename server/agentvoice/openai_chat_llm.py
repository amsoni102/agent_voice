"""OpenAI Chat Completions (gpt-4o-mini etc.) for Talk/practice — same shape as Ollama/Gemini clients."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from .ollama_llm import _window, speakable

log = logging.getLogger("agentvoice.openai_chat")


class OpenAIChatClient:
    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(
            timeout=60.0,
            headers={"Authorization": f"Bearer {api_key}"},
            base_url="https://api.openai.com/v1",
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def warmup(self) -> None:
        try:
            await self._client.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "ok"}],
                    "max_tokens": 1,
                },
            )
        except httpx.HTTPError as exc:
            log.warning("OpenAI chat warmup failed: %s", exc)

    def _messages(self, messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for msg in _window(messages, keep=14):
            role = msg.get("role") or "user"
            text = (msg.get("content") or "").strip()
            if role == "tool":
                out.append({"role": "user", "content": f"Tool {msg.get('tool_name') or 'tool'} result: {text}"})
            elif role in {"system", "user", "assistant"} and text:
                out.append({"role": role, "content": text})
        return out or [{"role": "user", "content": "Hello?"}]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: bool = True,
    ) -> tuple[str, list[dict[str, Any]]]:
        del tools
        resp = await self._client.post(
            "/chat/completions",
            json={
                "model": self.model,
                "messages": self._messages(messages),
                "temperature": 0.6,
                "max_tokens": 120,
            },
        )
        _raise_openai(resp)
        data = resp.json()
        text = speakable((((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or "")
        log.info("openai chat model=%s chars=%s", self.model, len(text))
        return text, []

    async def chat_stream(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        async with self._client.stream(
            "POST",
            "/chat/completions",
            json={
                "model": self.model,
                "messages": self._messages(messages),
                "temperature": 0.6,
                "max_tokens": 120,
                "stream": True,
            },
        ) as resp:
            if resp.status_code >= 400:
                body_text = (await resp.aread()).decode("utf-8", errors="replace")
                raise httpx.HTTPStatusError(
                    _openai_error_message(resp.status_code, body_text),
                    request=resp.request,
                    response=resp,
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = (((data.get("choices") or [{}])[0].get("delta") or {}).get("content")) or ""
                if delta:
                    yield delta

    async def summarize_json(self, system: str, user: str) -> dict[str, Any]:
        resp = await self._client.post(
            "/chat/completions",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system + "\nReply with JSON only."},
                    {"role": "user", "content": user[-12_000:]},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
                "max_tokens": 500,
            },
        )
        _raise_openai(resp)
        content = (((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content")) or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {}


def _openai_error_message(status: int, body_text: str) -> str:
    try:
        err = (json.loads(body_text).get("error") or {})
        msg = err.get("message") or body_text
        code = err.get("code") or ""
    except json.JSONDecodeError:
        msg, code = body_text, ""
    if status == 429 or "insufficient_quota" in code or "credit" in msg.lower():
        return (
            "OpenAI credits are depleted. Add billing at https://platform.openai.com/settings/organization/billing "
            f"or set VOICE_BACKEND=gemini / ollama. ({msg[:160]})"
        )
    return f"OpenAI HTTP {status}: {msg[:240]}"


def _raise_openai(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    raise httpx.HTTPStatusError(
        _openai_error_message(resp.status_code, resp.text),
        request=resp.request,
        response=resp,
    )
