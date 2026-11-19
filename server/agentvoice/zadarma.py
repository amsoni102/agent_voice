"""Zadarma phone line: the agent registers as a Zadarma SIP number and is a normal party on the call."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .db import now_iso
from .phone_bridge import PhoneBridge
from .sip import SipCall, SipCallFailed, SipConfig, SipUA

if TYPE_CHECKING:
    from .calls import CallManager

log = logging.getLogger("agentvoice.zadarma")

SIP_LINE_RATE = 8000
_FAIL_STATUS = {486: "busy", 600: "busy", 480: "no-answer", 408: "no-answer", 487: "canceled", 603: "busy"}


def sip_config(settings: Any) -> SipConfig:
    login = settings.zadarma_sip_login
    server = settings.zadarma_sip_server or ("pbx.zadarma.com" if "-" in login else "sip.zadarma.com")
    return SipConfig(
        username=login,
        password=settings.zadarma_sip_password,
        server=server,
        port=settings.zadarma_sip_port,
        local_port=settings.sip_local_port,
        public_ip=settings.sip_public_ip,
        rtp_ports=(settings.rtp_port_min, settings.rtp_port_max),
    )


def _e164(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    return f"+{digits}" if digits else number


class ZadarmaLine:
    def __init__(self, manager: "CallManager"):
        self.manager = manager
        self.ua = SipUA(sip_config(manager.settings), on_incoming=self._incoming)
        self.lines: dict[int, SipCall] = {}

    @property
    def status(self) -> dict[str, Any]:
        return {
            "registered": self.ua.registered,
            "login": self.ua.config.username,
            "server": self.ua.config.server,
            "error": self.ua.last_error,
        }

    async def start(self) -> None:
        await self.ua.start()

    async def stop(self) -> None:
        await self.ua.stop()

    def _bridge(self, call_row: dict[str, Any], sip_call: SipCall) -> PhoneBridge:
        async def hang() -> None:
            await sip_call.hangup()

        bridge = PhoneBridge(SIP_LINE_RATE, hang, label=f"call {call_row['id']}")
        sip_call.audio_source = bridge.next_frame
        sip_call.on_audio = bridge.receive
        return bridge

    async def _run(self, call_row: dict[str, Any], sip_call: SipCall, bridge: PhoneBridge, direction: str) -> None:
        """Agent session for an answered call; ends when either side hangs up."""
        db = self.manager.db
        session = self.manager.start_line_session(call_row, bridge, direction)
        self.lines[call_row["id"]] = sip_call
        try:
            starter = asyncio.create_task(session.start())
            reason = await sip_call.ended
            starter.cancel()
            log.info("call %s: line closed (%s)", call_row["id"], reason)
        finally:
            self.lines.pop(call_row["id"], None)
            current = db.get_call(call_row["id"]) or {}
            if current.get("status") not in ("completed", "failed", "busy", "no-answer", "canceled"):
                db.update_call(call_row["id"], status="completed", ended_at=current.get("ended_at") or now_iso())
            await session.stop("line closed")
            if call_row.get("lead_id"):
                self.manager._finalize_lead(db.get_call(call_row["id"]) or call_row)

    # ----------------------------------------------------------- inbound
    async def _incoming(self, sip_call: SipCall) -> None:
        manager = self.manager
        if not manager.settings.voice_available:
            sip_call.reject(480, "Temporarily Unavailable")
            return
        phone = _e164(sip_call.remote_number) if sip_call.remote_number else "unknown"
        lead = manager.db.find_lead_by_phone(phone)
        call_row = manager.db.create_call(
            phone=phone,
            lead_id=lead["id"] if lead else None,
            campaign_id=lead.get("campaign_id") if lead else None,
            direction="inbound",
            kind="inbound",
        )
        bridge = self._bridge(call_row, sip_call)
        await sip_call.answer()
        call_row = manager.db.update_call(
            call_row["id"], twilio_sid=f"sip:{sip_call.call_id}", status="in-progress", answered_at=now_iso()
        ) or call_row
        manager.spawn(self._run(call_row, sip_call, bridge, "inbound"))

    # ---------------------------------------------------------- outbound
    async def dial(self, call_row: dict[str, Any], phone: str) -> dict[str, Any]:
        db = self.manager.db
        if not self.ua.registered:
            raise RuntimeError(f"Zadarma SIP not registered ({self.ua.last_error or 'connecting'})")
        updated = db.update_call(call_row["id"], status="initiated") or call_row
        self.manager.spawn(self._outbound(updated, phone))
        return updated

    async def _outbound(self, call_row: dict[str, Any], phone: str) -> None:
        db = self.manager.db
        cid = call_row["id"]

        def ringing() -> None:
            db.update_call(cid, status="ringing")

        try:
            sip_call = await self.ua.call(phone, on_ringing=ringing)
        except SipCallFailed as exc:
            status = _FAIL_STATUS.get(exc.status, "failed")
            outcome = {"busy": "busy", "no-answer": "no_answer"}.get(status, "failed")
            row = db.update_call(cid, status=status, outcome=outcome, error=str(exc)[:500], ended_at=now_iso())
            log.info("call %s to %s not connected: %s", cid, phone, exc)
            if row and row.get("lead_id"):
                self.manager._finalize_lead(row)
            return
        row = db.update_call(cid, twilio_sid=f"sip:{sip_call.call_id}", status="in-progress", answered_at=now_iso())
        bridge = self._bridge(row or call_row, sip_call)
        await self._run(row or call_row, sip_call, bridge, "outbound")

    async def hangup(self, call_id: int) -> bool:
        sip_call = self.lines.get(call_id)
        if sip_call is None:
            return False
        await sip_call.hangup()
        return True
