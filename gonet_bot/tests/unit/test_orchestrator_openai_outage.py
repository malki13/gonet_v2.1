import asyncio
from datetime import datetime, timedelta, timezone

from packages.integrations.odoo_chat import OdooChatClient
from packages.integrations.openai_client import OpenAIClient
from packages.orchestrator.router import IntentRouter
from packages.orchestrator.service import OrchestratorService
from packages.orchestrator.session_context import SessionContextService
from packages.shared.schemas import InboundMessage


def test_orchestrator_directly_handoffs_when_openai_runtime_is_unavailable(monkeypatch):
    handoffs = []

    async def fake_escalate_new_client(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
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

    async def fail_router(self, message, state):
        raise AssertionError("router.decide should not run while OpenAI outage fallback is active")

    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_escalate_new_client)
    monkeypatch.setattr(IntentRouter, "decide", fail_router)

    OpenAIClient._runtime_outage_until = datetime.now(timezone.utc) + timedelta(seconds=120)
    OpenAIClient._runtime_last_error = "APITimeoutError: upstream unavailable"
    try:
        service = OrchestratorService()
        outbound = asyncio.run(
            service.handle_message(
                InboundMessage(
                    mensaje="q planes ofrecen",
                    channel="whatsapp",
                    recipient="593999",
                    session_id="orchestrator-openai-outage-1",
                )
            )
        )
        assert outbound.agent == "handoff"
        assert outbound.intent == "human_handoff"
        assert "asesor especializado" in outbound.message.lower()
        assert len(handoffs) == 1
        assert handoffs[0]["group"] == "iainfo"
        assert "openai" in (handoffs[0]["summary"] or "").lower()
        state = asyncio.run(
            SessionContextService().load(
                InboundMessage(
                    mensaje="",
                    channel="whatsapp",
                    recipient="593999",
                    session_id="orchestrator-openai-outage-1",
                )
            )
        )
        assert state is not None
        assert state.human_handoff is True
        assert state.metadata["handoff_group"] == "iainfo"
    finally:
        OpenAIClient.clear_runtime_failure()
