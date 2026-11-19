"""Cloud LLM via Google Gemini (Flash) for Talk/practice — same chat/stream shape as OllamaClient."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from .ollama_llm import _window, speakable

log = logging.getLogger("agentvoice.gemini")

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"


def _to_gemini_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Split system prompt + convert chat turns to Gemini contents."""
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for msg in _window(messages, keep=14):
        role = msg.get("role") or "user"
        text = (msg.get("content") or "").strip()
        if not text and role != "assistant":
            continue
        if role == "system":
            system_parts.append(text)
            continue
        if role == "tool":
            name = msg.get("tool_name") or "tool"
            contents.append({"role": "user", "parts": [{"text": f"Tool {name} result: {text}"}]})
            continue
        g_role = "model" if role == "assistant" else "user"
        # Gemini requires alternating user/model; merge consecutive same-role turns.
        if contents and contents[-1]["role"] == g_role:
            prev = contents[-1]["parts"][0]["text"]
            contents[-1]["parts"][0]["text"] = f"{prev}\n{text}".strip()
        else:
            contents.append({"role": g_role, "parts": [{"text": text}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": "Hello?"}]}]
    # API wants the first content role to be user.
    if contents[0]["role"] != "user":
        contents.insert(0, {"role": "user", "parts": [{"text": "Hello?"}]})
    return "\n".join(system_parts).strip(), contents


class GeminiClient:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(timeout=60.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def warmup(self) -> None:
        try:
            await self._client.post(
                f"{API_ROOT}/models/{self.model}:generateContent",
                params={"key": self.api_key},
                json={
                    "contents": [{"role": "user", "parts": [{"text": "ok"}]}],
                    "generationConfig": {"maxOutputTokens": 1, "temperature": 0},
                },
            )
        except httpx.HTTPError as exc:
            log.warning("Gemini warmup failed: %s", exc)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: bool = True,
    ) -> tuple[str, list[dict[str, Any]]]:
        # Tool calling stays rule-driven for Talk; Gemini is used for speech only.
        del tools
        system, contents = _to_gemini_messages(messages)
        body: dict[str, Any] = {"contents": contents, "generationConfig": _speech_config()}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        resp = await self._client.post(
            f"{API_ROOT}/models/{self.model}:generateContent",
            params={"key": self.api_key},
            json=body,
        )
        _raise_gemini(resp)
        data = resp.json()
        text = speakable(_extract_text(data))
        log.info("gemini chat model=%s chars=%s", self.model, len(text))
        return text, []

    async def chat_stream(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        system, contents = _to_gemini_messages(messages)
        body: dict[str, Any] = {"contents": contents, "generationConfig": _speech_config()}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{API_ROOT}/models/{self.model}:streamGenerateContent"
        async with self._client.stream(
            "POST", url, params={"key": self.api_key, "alt": "sse"}, json=body
        ) as resp:
            if resp.status_code >= 400:
                body_text = (await resp.aread()).decode("utf-8", errors="replace")
                raise httpx.HTTPStatusError(
                    _gemini_error_message(resp.status_code, body_text),
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
                piece = _extract_text(data)
                if piece:
                    yield piece

    async def summarize_json(self, system: str, user: str) -> dict[str, Any]:
        body = {
            "contents": [{"role": "user", "parts": [{"text": user[-12_000:]}]}],
            "systemInstruction": {"parts": [{"text": system + "\nReply with JSON only."}]},
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 500,
                "responseMimeType": "application/json",
            },
        }
        resp = await self._client.post(
            f"{API_ROOT}/models/{self.model}:generateContent",
            params={"key": self.api_key},
            json=body,
        )
        _raise_gemini(resp)
        content = _extract_text(resp.json()) or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {}


def _speech_config() -> dict[str, Any]:
    # Thinking tokens count against maxOutputTokens and delay the first word; a phone turn doesn't need them.
    return {
        "temperature": 0.8,
        "maxOutputTokens": 220,
        "topP": 0.95,
        "thinkingConfig": {"thinkingBudget": 0},
    }


def _gemini_error_message(status: int, body_text: str) -> str:
    try:
        msg = (json.loads(body_text).get("error") or {}).get("message") or body_text
    except json.JSONDecodeError:
        msg = body_text
    if status == 402 or "prepayment" in msg.lower() or "billing" in msg.lower():
        return (
            "Gemini credits are depleted. Top up at https://aistudio.google.com/ "
            f"or set VOICE_BACKEND=openai_chat / ollama. ({msg[:160]})"
        )
    return f"Gemini HTTP {status}: {msg[:240]}"


def _raise_gemini(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    raise httpx.HTTPStatusError(
        _gemini_error_message(resp.status_code, resp.text),
        request=resp.request,
        response=resp,
    )


def _extract_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for cand in data.get("candidates") or []:
        content = cand.get("content") or {}
        for part in content.get("parts") or []:
            text = part.get("text")
            if text:
                parts.append(text)
    return "".join(parts)
