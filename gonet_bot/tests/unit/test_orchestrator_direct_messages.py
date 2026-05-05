import asyncio

from packages.orchestrator.inactivity import InactivityService
from packages.orchestrator.service import OrchestratorService
from packages.shared.schemas import AgentResult, InboundMessage, SessionState


def test_orchestrator_system_failure_uses_response_composer(monkeypatch):
    service = OrchestratorService()
    calls = {"composed": False}

    async def fake_escalate_new_client(*, channel="internal", recipient="", summary=None, cedula=None, origen=None, group=None):
        return {"status": "sent"}

    async def fake_compose(**kwargs):
        calls["composed"] = True
        return kwargs["result"].model_copy(update={"message": "Derivacion renderizada"})

    async def fake_save(state):
        return None

    async def fake_sync_contact_registry(**kwargs):
        return None

    monkeypatch.setattr(service.handoff.odoo, "escalate_new_client", fake_escalate_new_client)
    monkeypatch.setattr(service.response_composer, "compose", fake_compose)
    monkeypatch.setattr(service.sessions, "save", fake_save)
    monkeypatch.setattr(service, "_sync_contact_registry", fake_sync_contact_registry)

    outbound = asyncio.run(
        service._handle_system_failure(
            message=InboundMessage(
                mensaje="mi internet no sirve",
                channel="whatsapp",
                recipient="593111",
                session_id="orchestrator-system-failure-1",
            ),
            state=SessionState(
                session_id="orchestrator-system-failure-1",
                channel="whatsapp",
                recipient="593111",
            ),
            exc=RuntimeError("boom"),
        )
    )

    assert calls["composed"] is True
    assert outbound.message == "Derivacion renderizada"


def test_orchestrator_system_failure_is_honest_when_handoff_creation_fails(monkeypatch):
    service = OrchestratorService()

    async def fake_escalate_new_client(*, channel="internal", recipient="", summary=None, cedula=None, origen=None, group=None):
        raise RuntimeError("odoo unavailable")

    async def fake_save(state):
        return None

    async def fake_sync_contact_registry(**kwargs):
        return None

    monkeypatch.setattr(service.handoff.odoo, "escalate_new_client", fake_escalate_new_client)
    monkeypatch.setattr(service.sessions, "save", fake_save)
    monkeypatch.setattr(service, "_sync_contact_registry", fake_sync_contact_registry)

    outbound = asyncio.run(
        service._handle_system_failure(
            message=InboundMessage(
                mensaje="mi internet no sirve",
                channel="whatsapp",
                recipient="593111",
                session_id="orchestrator-system-failure-2",
            ),
            state=SessionState(
                session_id="orchestrator-system-failure-2",
                channel="whatsapp",
                recipient="593111",
            ),
            exc=RuntimeError("boom"),
        )
    )

    assert outbound.agent == "clarify"
    assert outbound.intent == "clarify"
    assert "no pude dejar su caso con un asesor especializado" in outbound.message.lower()


def test_inactivity_user_notification_uses_response_composer(monkeypatch):
    service = InactivityService()
    delivered = {}

    async def fake_compose_direct_result(**kwargs):
        delivered["intent"] = kwargs["intent"]
        delivered["human_handoff"] = kwargs["metadata"]["human_handoff"]
        return AgentResult(message="Cierre renderizado", intent=kwargs["intent"], agent=kwargs["result_agent"])

    async def fake_deliver(*, channel, recipient, message, actions=None, media_type=None):
        delivered["channel"] = channel
        delivered["recipient"] = recipient
        delivered["message"] = message
        return {"status": "sent"}

    async def fake_close_contact(**kwargs):
        delivered["contact_closed"] = True
        return {"status": "ok"}

    async def fake_clear(**kwargs):
        delivered["session_cleared"] = kwargs["session_id"]
        return None

    monkeypatch.setattr(service.response_composer, "compose_direct_result", fake_compose_direct_result)
    monkeypatch.setattr(service.delivery, "deliver", fake_deliver)
    monkeypatch.setattr(service.contacts, "close_contact", fake_close_contact)
    monkeypatch.setattr(service.sessions, "clear", fake_clear)

    asyncio.run(
        service._close_inactive_session(
            SessionState(
                session_id="inactivity-direct-message-1",
                channel="whatsapp",
                recipient="593222",
            ),
            human_handoff=False,
        )
    )

    assert delivered["intent"] == "session_closed"
    assert delivered["human_handoff"] is False
    assert delivered["message"] == "Cierre renderizado"
    assert delivered["session_cleared"] == "inactivity-direct-message-1"


def test_inactivity_sales_close_uses_contextual_message(monkeypatch):
    service = InactivityService()
    delivered = {}

    async def fake_compose_direct_result(**kwargs):
        delivered["raw_message"] = kwargs["raw_message"]
        delivered["conversation_state"] = kwargs["metadata"]["response_plan"]["conversation_state"]
        return AgentResult(message=kwargs["raw_message"], intent=kwargs["intent"], agent=kwargs["result_agent"])

    async def fake_deliver(*, channel, recipient, message, actions=None, media_type=None):
        delivered["message"] = message
        return {"status": "sent"}

    async def fake_close_contact(**kwargs):
        return {"status": "ok"}

    async def fake_clear(**kwargs):
        return None

    monkeypatch.setattr(service.response_composer, "compose_direct_result", fake_compose_direct_result)
    monkeypatch.setattr(service.delivery, "deliver", fake_deliver)
    monkeypatch.setattr(service.contacts, "close_contact", fake_close_contact)
    monkeypatch.setattr(service.sessions, "clear", fake_clear)

    asyncio.run(
        service._close_inactive_session(
            SessionState(
                session_id="inactivity-direct-message-2",
                channel="whatsapp",
                recipient="593333",
                current_intent="commercial",
                last_agent="sales",
            ),
            human_handoff=False,
        )
    )

    assert "planes, cobertura o agencias" in delivered["raw_message"].lower()
    assert delivered["conversation_state"] == "session_closed_sales"
