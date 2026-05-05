import asyncio

from packages.agents.billing.service import BillingAgent
from packages.agents.sales.service import SalesAgent
from packages.agents.support.service import SupportAgent
from packages.shared.schemas import Attachment, InboundMessage, SessionState


def test_support_agent_internal_error_returns_response_plan(monkeypatch):
    agent = SupportAgent()
    handoffs = []

    async def boom(*, preferred_domain, message, state):
        raise RuntimeError("support exploded")

    async def fake_escalate_new_client(*, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(
            {
                "channel": channel,
                "recipient": recipient,
                "summary": summary,
                "cedula": cedula,
                "origen": origen,
                "group": group,
            }
        )
        return {"status": "sent", "channel_id": 7001}

    monkeypatch.setattr(agent.contact, "handle", boom)
    monkeypatch.setattr(agent.handoff, "escalate_new_client", fake_escalate_new_client)

    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="mi internet no sirve",
                channel="whatsapp",
                recipient="593111",
                session_id="support-error-plan-1",
            ),
            SessionState(session_id="support-error-plan-1"),
        )
    )

    assert result.agent == "handoff"
    assert result.intent == "human_handoff"
    assert result.metadata["response_plan"]["conversation_state"] == "handoff_created"
    assert result.metadata["response_plan"]["should_handoff"] is True
    assert "asesor especializado" in result.message.lower()
    assert handoffs[0]["group"] == "support"
    assert "support exploded" in (handoffs[0]["summary"] or "")


def test_billing_agent_internal_proof_error_returns_response_plan(monkeypatch):
    agent = BillingAgent()
    handoffs = []
    relays = []

    async def boom(*, preferred_domain, message, state):
        raise RuntimeError("billing exploded")

    async def fake_escalate_new_client(*, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(
            {
                "channel": channel,
                "recipient": recipient,
                "summary": summary,
                "cedula": cedula,
                "origen": origen,
                "group": group,
            }
        )
        return {"status": "sent", "internal_user": 17, "channel_id": 88}

    async def fake_relay_attachments(*, channel, recipient, attachments, cedula=None, origen=None, group=None, internal_user=None, channel_id=None):
        relays.append(
            {
                "channel": channel,
                "recipient": recipient,
                "attachments": attachments,
                "cedula": cedula,
                "origen": origen,
                "group": group,
                "internal_user": internal_user,
                "channel_id": channel_id,
            }
        )
        return [{"status": "sent"}]

    monkeypatch.setattr(agent.contact, "handle", boom)
    monkeypatch.setattr(agent.handoff, "escalate_new_client", fake_escalate_new_client)
    monkeypatch.setattr(agent.handoff, "relay_attachments", fake_relay_attachments)

    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="te envio el comprobante",
                channel="whatsapp",
                recipient="593111",
                session_id="billing-error-plan-1",
                attachments=[Attachment(type="image", mime_type="image/jpeg", filename="proof.jpg", url="https://example.com/proof.jpg")],
            ),
            SessionState(session_id="billing-error-plan-1"),
        )
    )

    assert result.agent == "handoff"
    assert result.intent == "human_handoff"
    assert result.metadata["response_plan"]["conversation_state"] == "handoff_created"
    assert result.metadata["response_plan"]["should_handoff"] is True
    assert "asesor especializado" in result.message.lower()
    assert handoffs[0]["group"] == "support"
    assert "billing exploded" in (handoffs[0]["summary"] or "")
    assert relays[0]["attachments"][0]["filename"] == "proof.jpg"
    assert relays[0]["internal_user"] == 17
    assert relays[0]["channel_id"] == 88


def test_sales_agent_internal_error_returns_response_plan(monkeypatch):
    agent = SalesAgent()
    state = SessionState(session_id="sales-error-plan-1")
    handoffs = []

    async def boom(message, state):
        raise OSError("sales exploded")

    async def fake_escalate_new_client(*, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(
            {
                "channel": channel,
                "recipient": recipient,
                "summary": summary,
                "cedula": cedula,
                "origen": origen,
                "group": group,
            }
        )
        return {"status": "sent", "channel_id": 9001}

    monkeypatch.setattr(agent, "_handle_internal", boom)
    monkeypatch.setattr(agent.handoff, "escalate_new_client", fake_escalate_new_client)

    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="quiero planes para mi casa",
                channel="whatsapp",
                recipient="593111",
                session_id="sales-error-plan-1",
            ),
            state,
        )
    )

    assert result.agent == "handoff"
    assert result.intent == "human_handoff"
    assert result.metadata["response_plan"]["conversation_state"] == "handoff_created"
    assert result.metadata["response_plan"]["should_handoff"] is True
    assert "asesor especializado" in result.message.lower()
    assert handoffs[0]["group"] == "iainfo"
    assert "sales exploded" in (handoffs[0]["summary"] or "")
