from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any, Mapping
from xml.sax.saxutils import escape, quoteattr

import httpx

from .config import Settings

log = logging.getLogger("agentvoice.twilio")

API = "https://api.twilio.com/2010-04-01"


class TwilioError(RuntimeError):
    pass


def compute_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def validate_signature(auth_token: str, url: str, params: Mapping[str, str], signature: str) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(compute_signature(auth_token, url, params), signature)


def stream_twiml(ws_url: str, parameters: Mapping[str, str]) -> str:
    params = "".join(
        f"<Parameter name={quoteattr(k)} value={quoteattr(str(v))}/>" for k, v in parameters.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Connect><Stream url={quoteattr(ws_url)}>{params}</Stream></Connect></Response>"
    )


def transfer_twiml(number: str, caller_id: str, message: str = "") -> str:
    say = f'<Say voice="Polly.Joanna">{escape(message)}</Say>' if message else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>{say}<Dial callerId={quoteattr(caller_id)} timeout=\"25\">{escape(number)}</Dial></Response>"
    )


def reject_twiml(message: str = "") -> str:
    say = f'<Say voice="Polly.Joanna">{escape(message)}</Say>' if message else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{say}<Hangup/></Response>'


class TwilioClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.AsyncClient(
            auth=(settings.twilio_account_sid, settings.twilio_auth_token), timeout=15.0
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _url(self, path: str) -> str:
        return f"{API}/Accounts/{self.settings.twilio_account_sid}/{path}"

    async def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.post(self._url(path), data=data)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message", resp.text)
            except ValueError:
                detail = resp.text
            raise TwilioError(f"Twilio {resp.status_code}: {detail}")
        return resp.json()

    async def create_call(self, *, to: str, call_id: int, machine_detection: str) -> str:
        base = self.settings.public_base_url
        data: dict[str, Any] = {
            "To": to,
            "From": self.settings.twilio_from_number,
            "Url": f"{base}/twilio/voice?call_id={call_id}",
            "Method": "POST",
            "StatusCallback": f"{base}/twilio/status?call_id={call_id}",
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
            "Timeout": 25,
        }
        if machine_detection:
            data.update(
                {
                    "MachineDetection": machine_detection,
                    "AsyncAmd": "true",
                    "AsyncAmdStatusCallback": f"{base}/twilio/amd?call_id={call_id}",
                    "AsyncAmdStatusCallbackMethod": "POST",
                }
            )
        result = await self._post("Calls.json", data)
        return result["sid"]

    async def hangup(self, sid: str) -> None:
        try:
            await self._post(f"Calls/{sid}.json", {"Status": "completed"})
        except TwilioError as exc:
            log.warning("hangup %s failed: %s", sid, exc)

    async def update_twiml(self, sid: str, twiml: str) -> None:
        await self._post(f"Calls/{sid}.json", {"Twiml": twiml})
