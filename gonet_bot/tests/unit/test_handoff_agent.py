import asyncio

import httpx

from packages.agents.handoff.service import HandoffAgent
from packages.shared.schemas import Attachment, InboundMessage, SessionState


def test_handoff_agent_routes_sales_request_to_commercial_group(monkeypatch):
    service = HandoffAgent()
    captured = {}

    async def fake_create_handoff(summary, *, channel="internal", recipient="", cedula=None, origen=None, group=None):
        captured["summary"] = summary
        captured["channel"] = channel
        captured["recipient"] = recipient
        captured["cedula"] = cedula
        captured["origen"] = origen
        captured["group"] = group
        return {"status": "sent"}

    monkeypatch.setattr(service.odoo, "create_handoff", fake_create_handoff)

    state = SessionState(session_id="handoff-sales-1", current_intent="commercial")
    state.metadata["sales"] = {"recommended_plan": {"name": "GoPlus"}}

    result = asyncio.run(
        service.handle(
            InboundMessage(
                mensaje="asesor",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
            ),
            state,
        )
    )

    assert result.intent == "human_handoff"
    assert "GoPlus" in result.message
    assert captured["group"] == "iainfo"
    assert "GoPlus" in captured["summary"]
    assert (result.metadata.get("response_plan") or {}).get("conversation_state") == "handoff_created"


def test_handoff_agent_relays_followup_messages_when_human_handoff_is_active(monkeypatch):
    service = HandoffAgent()
    relayed = {}

    async def fake_relay_customer_message(*, channel, recipient, message, cedula=None, origen=None, group=None, internal_user=None, channel_id=None):
        relayed["message"] = message
        relayed["group"] = group
        relayed["origen"] = origen
        return {"status": "sent"}

    async def fake_relay_attachments(*, channel, recipient, attachments, cedula=None, origen=None, group=None, internal_user=None, channel_id=None):
        relayed["attachments"] = attachments
        relayed["attachments_group"] = group
        return [{"status": "sent"}]

    monkeypatch.setattr(service.odoo, "relay_customer_message", fake_relay_customer_message)
    monkeypatch.setattr(service.odoo, "relay_attachments", fake_relay_attachments)

    state = SessionState(session_id="handoff-followup-1", current_intent="support", human_handoff=True)
    state.metadata["contact"] = {"selected_contract": "701177"}

    result = asyncio.run(
        service.handle(
            InboundMessage(
                mensaje="sigue igual",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                attachments=[
                    Attachment(
                        type="image",
                        mime_type="image/jpeg",
                        filename="foto.jpg",
                        url="https://example.com/foto.jpg",
                    )
                ],
            ),
            state,
        )
    )

    assert result.intent == "human_handoff"
    assert result.metadata.get("skip_delivery") is True
    assert relayed["message"] == "sigue igual"
    assert relayed["group"] == "support"
    assert relayed["attachments"][0]["filename"] == "foto.jpg"


def test_handoff_agent_is_honest_when_odoo_handoff_fails(monkeypatch):
    service = HandoffAgent()

    async def fake_create_handoff(summary, *, channel="internal", recipient="", cedula=None, origen=None, group=None):
        raise RuntimeError("odoo unavailable")

    monkeypatch.setattr(service.odoo, "create_handoff", fake_create_handoff)

    state = SessionState(session_id="handoff-failed-1", current_intent="support")
    state.metadata["contact"] = {"selected_contract": "701177"}

    result = asyncio.run(
        service.handle(
            InboundMessage(
                mensaje="asesor",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
            ),
            state,
        )
    )

    assert result.intent == "clarify"
    assert "no pude completar esa derivación" in result.message.lower()
    assert (result.metadata.get("response_plan") or {}).get("conversation_state") == "handoff_failed"


def test_handoff_agent_treats_odoo_timeout_as_handoff(monkeypatch):
    service = HandoffAgent()

    async def fake_create_handoff(summary, *, channel="internal", recipient="", cedula=None, origen=None, group=None):
        raise httpx.ReadTimeout("timeout", request=httpx.Request("POST", "https://example.com"))

    monkeypatch.setattr(service.odoo, "create_handoff", fake_create_handoff)

    state = SessionState(session_id="handoff-timeout-1", current_intent="support")
    state.metadata["contact"] = {"selected_contract": "701177"}

    result = asyncio.run(
        service.handle(
            InboundMessage(
                mensaje="asesor",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
            ),
            state,
        )
    )

    assert result.intent == "human_handoff"
    assert result.metadata.get("handoff_timeout") is True
    assert "volver a escribirme" not in result.message.lower()
