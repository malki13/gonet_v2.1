import asyncio

from packages.orchestrator.service import OrchestratorService
from packages.orchestrator.state_machine import update_state
from packages.shared.schemas import AgentResult, FlowTurnInterpretation, InboundMessage, RouteDecision, SessionState


def _build_pending_identity_state(*, session_id: str, pending_agent: str) -> SessionState:
    state = SessionState(
        session_id=session_id,
        channel="whatsapp",
        recipient="593111",
        current_intent="ask_cedula",
        last_agent="clarify",
    )
    state.awaiting_field = "cedula"
    state.metadata["pending_agent"] = pending_agent
    state.metadata["pending_message"] = "quiero registrar un pago"
    state.history.append({"role": "assistant", "content": "Para revisarlo necesito la cédula o RUC del titular."})
    return state


def test_update_state_keeps_pending_message_while_identity_turn_finishes():
    state = SessionState(
        session_id="orchestrator-state-machine-keep-pending-message-1",
        channel="whatsapp",
        recipient="593111",
        current_intent="ask_cedula",
        last_agent="clarify",
    )
    state.awaiting_field = "cedula"
    state.metadata["pending_agent"] = "support"
    state.metadata["pending_message"] = "no tengo internet"

    decision = RouteDecision(
        agent="support",
        intent="support",
        confidence=0.99,
        reason="pending_identity_resolved",
    )
    result = AgentResult(
        message="Ya revisé su contrato.",
        intent="support",
        agent="support",
    )

    updated = update_state(state, decision, result)

    assert updated.awaiting_field is None
    assert updated.metadata.get("pending_agent") is None
    assert updated.metadata.get("pending_message") == "no tengo internet"


def test_orchestrator_pending_identity_can_switch_to_sales(monkeypatch):
    service = OrchestratorService()
    state = _build_pending_identity_state(session_id="orchestrator-contextual-identity-sales-1", pending_agent="billing")
    saved = {}

    async def fake_load(message):
        return state

    async def fake_save(updated_state):
        saved["state"] = updated_state

    async def fake_noop(*args, **kwargs):
        return None

    async def fake_compose(**kwargs):
        return kwargs["result"]

    async def fake_sales_handle(message, state):
        return AgentResult(message="Te ayudo con planes.", intent="commercial", agent="sales")

    async def fake_interpret(**kwargs):
        return FlowTurnInterpretation(
            action="switch_intent",
            target_intent="sales",
            confidence=0.94,
            reason="switch_to_sales",
        )

    monkeypatch.setattr(service.sessions, "load", fake_load)
    monkeypatch.setattr(service.sessions, "save", fake_save)
    monkeypatch.setattr(service.contact_registry, "touch_contact", fake_noop)
    monkeypatch.setattr(service.contact_registry, "mark_ai_active", fake_noop)
    monkeypatch.setattr(service.response_composer, "compose", fake_compose)
    monkeypatch.setattr(service.sales, "handle", fake_sales_handle)
    monkeypatch.setattr(service.turn_interpreter, "interpret", fake_interpret)

    outbound = asyncio.run(
        service.handle_message(
            InboundMessage(
                mensaje="mejor quiero planes",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
            )
        )
    )

    assert outbound.agent == "sales"
    assert outbound.intent == "commercial"
    assert saved["state"].awaiting_field is None
    assert saved["state"].metadata.get("pending_agent") is None


def test_orchestrator_pending_identity_switches_to_sales_for_typoed_discovery(monkeypatch):
    service = OrchestratorService()
    state = _build_pending_identity_state(session_id="orchestrator-contextual-identity-sales-typo-1", pending_agent="billing")
    saved = {}

    async def fake_load(message):
        return state

    async def fake_save(updated_state):
        saved["state"] = updated_state

    async def fake_noop(*args, **kwargs):
        return None

    async def fake_compose(**kwargs):
        return kwargs["result"]

    async def fake_sales_handle(message, state):
        return AgentResult(message="Te ayudo con planes.", intent="commercial", agent="sales")

    async def fake_interpret(**kwargs):
        return kwargs["fallback"]

    monkeypatch.setattr(service.sessions, "load", fake_load)
    monkeypatch.setattr(service.sessions, "save", fake_save)
    monkeypatch.setattr(service.contact_registry, "touch_contact", fake_noop)
    monkeypatch.setattr(service.contact_registry, "mark_ai_active", fake_noop)
    monkeypatch.setattr(service.response_composer, "compose", fake_compose)
    monkeypatch.setattr(service.sales, "handle", fake_sales_handle)
    monkeypatch.setattr(service.turn_interpreter, "interpret", fake_interpret)

    outbound = asyncio.run(
        service.handle_message(
            InboundMessage(
                mensaje="queee intrsnet ofreces?",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
            )
        )
    )

    assert outbound.agent == "sales"
    assert outbound.intent == "commercial"
    assert saved["state"].awaiting_field is None
    assert saved["state"].metadata.get("pending_agent") is None


def test_orchestrator_pending_identity_handoffs_quietly_when_user_lacks_holder_document(monkeypatch):
    service = OrchestratorService()
    state = _build_pending_identity_state(session_id="orchestrator-contextual-identity-handoff-1", pending_agent="support")
    saved = {}
    handoffs = []

    async def fake_load(message):
        return state

    async def fake_save(updated_state):
        saved["state"] = updated_state

    async def fake_noop(*args, **kwargs):
        return None

    async def fake_compose(**kwargs):
        return kwargs["result"]

    async def fake_interpret(**kwargs):
        return kwargs["fallback"]

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
        return {"status": "sent"}

    monkeypatch.setattr(service.sessions, "load", fake_load)
    monkeypatch.setattr(service.sessions, "save", fake_save)
    monkeypatch.setattr(service.contact_registry, "touch_contact", fake_noop)
    monkeypatch.setattr(service.contact_registry, "mark_ai_active", fake_noop)
    monkeypatch.setattr(service.response_composer, "compose", fake_compose)
    monkeypatch.setattr(service.turn_interpreter, "interpret", fake_interpret)
    monkeypatch.setattr(service.handoff.odoo, "escalate_new_client", fake_escalate_new_client)

    outbound = asyncio.run(
        service.handle_message(
            InboundMessage(
                mensaje="No tengo la cédula del titular",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
            )
        )
    )

    assert outbound.agent == "handoff"
    assert outbound.intent == "human_handoff"
    assert "dame un momento" in outbound.message.lower()
    assert "asesor especializado" in outbound.message.lower()
    assert len(handoffs) == 1
    assert handoffs[0]["group"] == "support"
    assert "no cuenta con la cédula o ruc del titular" in (handoffs[0]["summary"] or "").lower()
    assert saved["state"].human_handoff is True


def test_orchestrator_pending_identity_handoffs_after_cedula_for_assistant_request(monkeypatch):
    service = OrchestratorService()
    state = _build_pending_identity_state(session_id="orchestrator-contextual-identity-handoff-cedula-1", pending_agent="handoff")
    saved = {}
    handoffs = []

    async def fake_load(message):
        return state

    async def fake_save(updated_state):
        saved["state"] = updated_state

    async def fake_noop(*args, **kwargs):
        return None

    async def fake_compose(**kwargs):
        return kwargs["result"]

    async def fake_create_handoff(summary, *, channel="internal", recipient="", cedula=None, origen=None, group=None):
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
        return {"status": "sent"}

    monkeypatch.setattr(service.sessions, "load", fake_load)
    monkeypatch.setattr(service.sessions, "save", fake_save)
    monkeypatch.setattr(service.contact_registry, "touch_contact", fake_noop)
    monkeypatch.setattr(service.contact_registry, "mark_ai_active", fake_noop)
    monkeypatch.setattr(service.response_composer, "compose", fake_compose)
    monkeypatch.setattr(service.handoff.odoo, "create_handoff", fake_create_handoff)

    outbound = asyncio.run(
        service.handle_message(
            InboundMessage(
                mensaje="0102030405",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            )
        )
    )

    assert outbound.agent == "handoff"
    assert outbound.intent == "human_handoff"
    assert len(handoffs) == 1
    assert handoffs[0]["cedula"] == "0102030405"
    assert saved["state"].human_handoff is True


def test_orchestrator_human_request_without_identity_asks_for_cedula(monkeypatch):
    service = OrchestratorService()
    saved = {}
    handoff_called = []

    async def fake_load(message):
        return SessionState(session_id=message.session_id, channel=message.channel, recipient=message.recipient)

    async def fake_save(updated_state):
        saved["state"] = updated_state

    async def fake_noop(*args, **kwargs):
        return None

    async def fake_compose(**kwargs):
        return kwargs["result"]

    async def fake_handoff(*args, **kwargs):
        handoff_called.append(True)
        return AgentResult(message="no debería llamarse", intent="human_handoff", agent="handoff")

    monkeypatch.setattr(service.sessions, "load", fake_load)
    monkeypatch.setattr(service.sessions, "save", fake_save)
    monkeypatch.setattr(service.contact_registry, "touch_contact", fake_noop)
    monkeypatch.setattr(service.contact_registry, "mark_ai_active", fake_noop)
    monkeypatch.setattr(service.contact_registry, "mark_human_active", fake_noop)
    monkeypatch.setattr(service.response_composer, "compose", fake_compose)
    monkeypatch.setattr(service.handoff, "handle", fake_handoff)

    outbound = asyncio.run(
        service.handle_message(
            InboundMessage(
                mensaje="hola un asesor por favor",
                channel="whatsapp",
                recipient="593111",
                session_id="orchestrator-human-request-no-identity-1",
            )
        )
    )

    assert outbound.intent == "ask_cedula"
    assert "cédula" in outbound.message.lower() or "cedula" in outbound.message.lower()
    assert not handoff_called
    assert saved["state"].awaiting_field == "cedula"
    assert saved["state"].metadata.get("pending_agent") == "handoff"


def test_orchestrator_support_no_service_followup_persists_and_escalates(monkeypatch):
    service = OrchestratorService()
    persisted = {}
    handoffs = []

    async def fake_load(message):
        raw = persisted.get(message.session_id)
        if raw is None:
            return SessionState(session_id=message.session_id, channel=message.channel, recipient=message.recipient)
        return SessionState.model_validate(raw)

    async def fake_save(updated_state):
        persisted[updated_state.session_id] = updated_state.model_dump(mode="json")

    async def fake_noop(*args, **kwargs):
        return None

    async def fake_compose(**kwargs):
        return kwargs["result"]

    async def fake_router_decide(message, state):
        return RouteDecision(
            agent="support",
            intent="support",
            confidence=0.99,
            reason="test_support_route",
        )

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "500007",
                    "state": "active",
                    "status_label": "activo",
                    "partner_name": "Christian Montero",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 2}}}

    async def fake_onu_status(self, contrato: str):
        return {"ok": True, "status": "working", "power_dbm": -20.22}

    async def fake_handoff(*, channel, recipient, summary=None, cedula=None, origen=None, group=None):
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
        return {"status": "sent"}

    monkeypatch.setattr(service.sessions, "load", fake_load)
    monkeypatch.setattr(service.sessions, "save", fake_save)
    monkeypatch.setattr(service.contact_registry, "touch_contact", fake_noop)
    monkeypatch.setattr(service.contact_registry, "mark_ai_active", fake_noop)
    monkeypatch.setattr(service.contact_registry, "mark_human_active", fake_noop)
    monkeypatch.setattr(service.response_composer, "compose", fake_compose)
    monkeypatch.setattr(service.router, "decide", fake_router_decide)
    monkeypatch.setattr("packages.integrations.contract_lookup.ContractLookupClient.info_personal_by_cedula", fake_info)
    monkeypatch.setattr(service.support.contact.smart, "enabled", lambda: True)
    monkeypatch.setattr(service.support.contact.smart, "monitor_contract", fake_monitor.__get__(service.support.contact.smart, type(service.support.contact.smart)))
    monkeypatch.setattr(service.support.contact.onu, "enabled", lambda: True)
    monkeypatch.setattr(service.support.contact.onu, "get_status", fake_onu_status.__get__(service.support.contact.onu, type(service.support.contact.onu)))
    monkeypatch.setattr(service.support.contact.handoff, "escalate_new_client", fake_handoff)

    first = asyncio.run(
        service.handle_message(
            InboundMessage(
                mensaje="mi internet no vale",
                channel="whatsapp",
                recipient="593111",
                session_id="orchestrator-support-no-service-escalation-1",
            )
        )
    )
    second = asyncio.run(
        service.handle_message(
            InboundMessage(
                mensaje="0102030405",
                channel="whatsapp",
                recipient="593111",
                session_id="orchestrator-support-no-service-escalation-1",
                cedula="0102030405",
            )
        )
    )
    third = asyncio.run(
        service.handle_message(
            InboundMessage(
                mensaje="ACEPTO",
                channel="whatsapp",
                recipient="593111",
                session_id="orchestrator-support-no-service-escalation-1",
            )
        )
    )
    fourth = asyncio.run(
        service.handle_message(
            InboundMessage(
                mensaje="no sirve",
                channel="whatsapp",
                recipient="593111",
                session_id="orchestrator-support-no-service-escalation-1",
            )
        )
    )

    assert first.intent == "ask_cedula"
    assert second.intent == "consent_required"
    assert third.intent == "support_network_monitoring"
    assert fourth.intent == "human_handoff"
    assert handoffs
    assert "500007" in (handoffs[0]["summary"] or "")


def test_orchestrator_pending_identity_can_switch_contact_domain(monkeypatch):
    service = OrchestratorService()
    state = _build_pending_identity_state(session_id="orchestrator-contextual-identity-support-1", pending_agent="billing")
    saved = {}

    async def fake_load(message):
        return state

    async def fake_save(updated_state):
        saved["state"] = updated_state

    async def fake_noop(*args, **kwargs):
        return None

    async def fake_compose(**kwargs):
        return kwargs["result"]

    async def fake_interpret(**kwargs):
        return FlowTurnInterpretation(
            action="switch_intent",
            target_intent="support",
            confidence=0.91,
            reason="switch_to_support",
        )

    monkeypatch.setattr(service.sessions, "load", fake_load)
    monkeypatch.setattr(service.sessions, "save", fake_save)
    monkeypatch.setattr(service.contact_registry, "touch_contact", fake_noop)
    monkeypatch.setattr(service.contact_registry, "mark_ai_active", fake_noop)
    monkeypatch.setattr(service.response_composer, "compose", fake_compose)
    monkeypatch.setattr(service.turn_interpreter, "interpret", fake_interpret)

    outbound = asyncio.run(
        service.handle_message(
            InboundMessage(
                mensaje="es por mi internet",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
            )
        )
    )

    assert outbound.agent == "clarify"
    assert outbound.intent == "ask_cedula"
    assert saved["state"].awaiting_field == "cedula"
    assert saved["state"].metadata.get("pending_agent") == "support"
