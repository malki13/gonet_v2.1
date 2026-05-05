import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from packages.agents.contact_flow import ContactFlowService
from packages.shared.assistant_persona import assistant_intro_prefix
from packages.shared.schemas import AgentResult, Attachment, InboundMessage, SessionState


def test_contact_flow_understands_contract_selection_phrase(monkeypatch):
    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {"code": "701177", "state": "cortado", "partner_name": "Nancy Mercedes", "residual": "164.94"},
                {"code": "800003", "state": "cortado", "partner_name": "Nancy Mercedes", "residual": "116.16"},
            ],
        }

    monkeypatch.setattr("packages.integrations.contract_lookup.ContractLookupClient.info_personal_by_cedula", fake_info)

    service = ContactFlowService()
    state = SessionState(session_id="contact-flow-contract-phrase-1")

    first = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="Facturación",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )
    assert first.intent == "contract_selection"

    second = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="el contrato 2",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert second.intent == "consent_required"
    assert (state.metadata.get("contact") or {}).get("selected_contract") == "800003"


def test_contact_flow_requests_identity_before_handoff(monkeypatch):
    handoffs = []

    async def fake_handoff(*, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(
            {
                "channel": channel,
                "recipient": recipient,
                "summary": summary,
                "cedula": cedula,
                "origen": origen,
            }
        )
        return {"status": "sent"}

    service = ContactFlowService()
    monkeypatch.setattr(service.handoff, "escalate_new_client", fake_handoff)
    state = SessionState(session_id="contact-flow-asesor-1")

    first = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="asesor",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
            ),
            state=state,
        )
    )

    assert first.intent == "ask_cedula"
    assert "cédula o ruc" in first.message.lower()
    assert (first.metadata.get("response_plan") or {}).get("conversation_state") == "identity_request_handoff"
    assert handoffs == []

    second = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="0102030405",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert second.intent == "human_handoff"
    assert "asesor especializado" in second.message.lower()
    assert len(handoffs) == 1
    assert handoffs[0]["cedula"] == "0102030405"


def test_contact_flow_is_honest_when_bare_handoff_fails(monkeypatch):
    async def fake_handoff(**kwargs):
        raise RuntimeError("odoo down")

    service = ContactFlowService()
    monkeypatch.setattr(service.handoff, "escalate_new_client", fake_handoff)
    state = SessionState(session_id="contact-flow-asesor-failed-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["selected_contract"] = "800003"

    result = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="asesor",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert result.intent == "clarify"
    assert "no pude completar esa derivación" in result.message.lower()
    assert (result.metadata.get("response_plan") or {}).get("conversation_state") == "handoff_failed"


def test_contact_flow_contract_not_found_uses_response_plan(monkeypatch):
    async def fake_info(self, cedula: str):
        return {"ok": True, "data": []}

    monkeypatch.setattr("packages.integrations.contract_lookup.ContractLookupClient.info_personal_by_cedula", fake_info)

    service = ContactFlowService()
    state = SessionState(session_id="contact-flow-contract-not-found-1")

    result = asyncio.run(
        service.handle(
            preferred_domain="support",
            message=InboundMessage(
                mensaje="mi internet no sirve",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert result.intent == "ask_cedula"
    assert (result.metadata.get("response_plan") or {}).get("conversation_state") == "identity_contract_not_found"
    assert "no encontré" in result.message.lower() or "no encontre" in result.message.lower()


def test_contact_flow_handoffs_when_user_cannot_provide_holder_document(monkeypatch):
    handoffs = []

    async def fake_handoff(*, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(
            {
                "channel": channel,
                "recipient": recipient,
                "summary": summary,
                "cedula": cedula,
                "origen": origen,
            }
        )
        return {"status": "sent"}

    service = ContactFlowService()
    monkeypatch.setattr(service.handoff, "escalate_new_client", fake_handoff)
    state = SessionState(session_id="contact-flow-no-holder-doc-1")

    result = asyncio.run(
        service.handle(
            preferred_domain="support",
            message=InboundMessage(
                mensaje="No tengo la cédula del titular",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
            ),
            state=state,
        )
    )

    assert result.intent == "human_handoff"
    assert "dame un momento" in result.message.lower()
    assert "asesor especializado" in result.message.lower()
    assert len(handoffs) == 1
    assert "no cuenta con cédula o ruc del titular" in (handoffs[0]["summary"] or "").lower()


def test_assistant_cordial_intro_uses_ecuador_time_greeting():
    profile = {"greeting_style": "cordial"}

    intro = assistant_intro_prefix(
        assistant_name="Andres",
        assistant_profile=profile,
        now=datetime(2026, 4, 16, 9, 15, tzinfo=ZoneInfo("America/Guayaquil")),
    )

    assert intro.startswith("Buenos días, soy Andres de GoNet.")
    assert "hola" not in intro.lower()


def test_contact_flow_handoffs_after_two_contract_lookup_failures(monkeypatch):
    handoffs = []

    async def fake_info(self, cedula: str):
        return {"ok": True, "data": []}

    async def fake_handoff(*, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(
            {
                "channel": channel,
                "recipient": recipient,
                "summary": summary,
                "cedula": cedula,
                "origen": origen,
            }
        )
        return {"status": "sent"}

    monkeypatch.setattr("packages.integrations.contract_lookup.ContractLookupClient.info_personal_by_cedula", fake_info)

    service = ContactFlowService()
    monkeypatch.setattr(service.handoff, "escalate_new_client", fake_handoff)
    state = SessionState(session_id="contact-flow-contract-not-found-handoff-1")

    first = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="Quiero facturación",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
            ),
            state=state,
        )
    )
    assert first.intent == "ask_cedula"

    second = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="0102030405",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )
    assert second.intent == "ask_cedula"

    third = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="0102030406",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030406",
            ),
            state=state,
        )
    )

    assert third.intent == "human_handoff"
    assert "dame un momento" in third.message.lower()
    assert "asesor especializado" in third.message.lower()
    assert len(handoffs) == 1
    assert "después de 2 intentos" in (handoffs[0]["summary"] or "").lower()
    assert handoffs[0]["cedula"] == "0102030406"


def test_contact_flow_uses_short_billing_nudge_while_awaiting_action():
    service = ContactFlowService()
    state = SessionState(session_id="contact-flow-billing-nudge-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "800003",
            "state": "cortado",
            "status_label": "cortado",
            "partner_name": "Nancy Mercedes",
            "residual": "116.16",
        }
    ]
    contact_state["selected_contract"] = "800003"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "billing"
    contact_state["billing"] = {
        "awaiting_action": True,
        "awaiting_proof": False,
        "proof_attempts": 0,
        "proof_failures": [],
        "processing_async": False,
    }

    result = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="ok",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert result.intent == "billing_action_clarify"
    assert "registrar pago" in result.message.lower()
    assert "link de cobro" in result.message.lower()
    assert "asesor especializado" in result.message.lower()
    assert "valor pendiente" not in result.message.lower()


def test_contact_flow_understands_natural_billing_option_reference():
    service = ContactFlowService()
    state = SessionState(session_id="contact-flow-billing-natural-option-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "800003",
            "state": "cortado",
            "status_label": "cortado",
            "partner_name": "Nancy Mercedes",
            "residual": "116.16",
        }
    ]
    contact_state["selected_contract"] = "800003"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "billing"
    contact_state["billing"] = {
        "awaiting_action": True,
        "awaiting_proof": False,
        "proof_attempts": 0,
        "proof_failures": [],
        "processing_async": False,
        "pending_proof_attachments": [],
    }

    result = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="la primera",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert result.intent == "billing_proof_requested"
    lowered = result.message.lower()
    assert "comprobante" in lowered
    assert "numero del documento" in lowered or "número del documento" in lowered
    assert "fecha" in lowered
    assert "monto" in lowered


def test_contact_flow_reuses_cached_proof_attachment_when_billing_resumes(monkeypatch):
    service = ContactFlowService()
    state = SessionState(session_id="contact-flow-proof-cache-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "800003",
            "state": "active",
            "status_label": "activo",
            "partner_name": "Nancy Mercedes",
            "residual": "116.16",
        }
    ]
    contact_state["selected_contract"] = "800003"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "billing"
    contact_state["billing"] = {
        "awaiting_action": False,
        "awaiting_proof": False,
        "proof_attempts": 0,
        "proof_failures": [],
        "processing_async": False,
        "pending_proof_attachments": [
            {
                "type": "image",
                "mime_type": "image/jpeg",
                "filename": "proof.jpg",
                "url": "https://bot.example/media/proof.jpg",
            }
        ],
    }
    captured = {}

    async def fake_handle_billing(*, message, state, contract, just_selected_contract=False):
        captured["attachments"] = message.attachments
        captured["text"] = message.mensaje
        return AgentResult(message="ok", intent="billing_proof_pending", agent="billing")

    monkeypatch.setattr(service, "_handle_billing", fake_handle_billing)

    result = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="sí",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert result.intent == "billing_proof_pending"
    assert len(captured["attachments"]) == 1
    assert captured["attachments"][0].filename == "proof.jpg"


def test_contact_flow_billing_handoff_relays_payment_attachment(monkeypatch):
    handoffs = []
    relayed = []

    async def fake_handoff(*, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(summary)
        return {"status": "sent"}

    async def fake_relay_attachments(*, channel, recipient, attachments, cedula=None, origen=None, group=None, internal_user=None, channel_id=None):
        relayed.append(
            {
                "channel": channel,
                "recipient": recipient,
                "attachments": attachments,
            }
        )
        return [{"status": "sent"}]

    service = ContactFlowService()
    monkeypatch.setattr(service.handoff, "escalate_new_client", fake_handoff)
    monkeypatch.setattr(service.handoff, "relay_attachments", fake_relay_attachments)

    result = asyncio.run(
        service._billing_handoff(
            message=InboundMessage(
                mensaje="Adjunto comprobante",
                channel="whatsapp",
                recipient="593111",
                session_id="contact-flow-billing-handoff-1",
                attachments=[
                    Attachment(
                        type="image",
                        mime_type="image/jpeg",
                        filename="proof.jpg",
                        base64_data="ZmFrZQ==",
                    )
                ],
            ),
            summary="Resumen prueba",
            final="Voy a dejar tu caso con un asesor especializado.",
        )
    )

    assert result.intent == "billing_handoff"
    assert handoffs == ["Resumen prueba"]
    assert relayed
    assert relayed[0]["attachments"][0]["filename"] == "proof.jpg"


def test_contact_flow_billing_handoff_is_honest_when_odoo_fails(monkeypatch):
    async def fake_handoff(**kwargs):
        raise RuntimeError("odoo unavailable")

    service = ContactFlowService()
    monkeypatch.setattr(service.handoff, "escalate_new_client", fake_handoff)

    result = asyncio.run(
        service._billing_handoff(
            message=InboundMessage(
                mensaje="Adjunto comprobante",
                channel="whatsapp",
                recipient="593111",
                session_id="contact-flow-billing-handoff-failed-1",
            ),
            summary="Resumen prueba",
            final="Voy a dejar tu caso con un asesor especializado.",
        )
    )

    assert result.intent == "billing_handoff_failed"
    assert "no pude completar esa derivación" in result.message.lower()
    assert (result.metadata.get("response_plan") or {}).get("conversation_state") == "handoff_failed"


def test_contact_flow_uses_active_turn_interpreter_for_ambiguous_consent(monkeypatch):
    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {"code": "800003", "state": "cortado", "partner_name": "Nancy Mercedes", "residual": "116.16"},
            ],
        }

    class FakeLLM:
        def enabled(self):
            return True

        async def extract_json(self, **kwargs):
            payload = (kwargs or {}).get("payload") or {}
            flow_context = payload.get("flow_context") or {}
            message = (payload.get("message") or {}).get("normalized")
            if flow_context.get("current_stage") == "consent" and message == "dale nomas":
                return {
                    "status": "ok",
                    "result": {
                        "action": "accept_information",
                        "confidence": 0.94,
                        "reason": "consent_in_context",
                        "slot_updates": {"consent": True},
                    },
                }
            return {"status": "ok", "result": {}}

    monkeypatch.setattr("packages.integrations.contract_lookup.ContractLookupClient.info_personal_by_cedula", fake_info)

    service = ContactFlowService(llm=FakeLLM())
    state = SessionState(session_id="contact-flow-consent-contextual-1")

    first = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="Facturación",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )
    assert first.intent == "consent_required"
    assert (first.metadata.get("response_plan") or {}).get("conversation_state") == "information_consent"

    second = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="dale nomas",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert second.intent == "billing"
    lowered = second.message.lower()
    assert "si ya realizó el pago" in lowered
    assert "si prefiere pagar ahora" in lowered
    assert "https://pagos.gonet.ec/payment" in lowered


def test_contact_flow_uses_active_turn_interpreter_for_billing_action(monkeypatch):
    class FakeLLM:
        def enabled(self):
            return True

        async def extract_json(self, **kwargs):
            payload = (kwargs or {}).get("payload") or {}
            flow_context = payload.get("flow_context") or {}
            message = (payload.get("message") or {}).get("normalized")
            if flow_context.get("current_stage") == "billing_action" and message == "te paso el recibo entonces":
                return {
                    "status": "ok",
                    "result": {
                        "action": "answer_current_step",
                        "confidence": 0.93,
                        "reason": "billing_action_in_context",
                        "slot_updates": {"billing_action": "register_payment"},
                    },
                }
            return {"status": "ok", "result": {}}

    service = ContactFlowService(llm=FakeLLM())
    state = SessionState(session_id="contact-flow-billing-action-contextual-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "800003",
            "state": "cortado",
            "status_label": "cortado",
            "partner_name": "Nancy Mercedes",
            "residual": "116.16",
        }
    ]
    contact_state["selected_contract"] = "800003"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "billing"
    contact_state["billing"] = {
        "awaiting_action": True,
        "awaiting_proof": False,
        "proof_attempts": 0,
        "proof_failures": [],
        "processing_async": False,
        "pending_proof_attachments": [],
    }

    result = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="te paso el recibo entonces",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert result.intent == "billing_proof_requested"
    lowered = result.message.lower()
    assert "envieme el comprobante" in lowered or "envíeme el comprobante" in lowered
    assert "numero del documento" in lowered or "número del documento" in lowered
    assert "fecha" in lowered
    assert "monto" in lowered


def test_contact_flow_uses_active_turn_interpreter_for_support_followup(monkeypatch):
    handoffs = []

    async def fake_handoff(*, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(summary)
        return {"status": "sent"}

    class FakeLLM:
        def enabled(self):
            return True

        async def extract_json(self, **kwargs):
            payload = (kwargs or {}).get("payload") or {}
            flow_context = payload.get("flow_context") or {}
            message = (payload.get("message") or {}).get("normalized")
            if flow_context.get("current_stage") == "support_resolution_confirmation" and message == "la verdad sigue molestando":
                return {
                    "status": "ok",
                    "result": {
                        "action": "answer_current_step",
                        "confidence": 0.95,
                        "reason": "support_followup_in_context",
                        "slot_updates": {"resolution": "persists"},
                    },
                }
            return {"status": "ok", "result": {}}

    service = ContactFlowService(llm=FakeLLM())
    monkeypatch.setattr(service.handoff, "escalate_new_client", fake_handoff)

    state = SessionState(session_id="contact-flow-support-followup-contextual-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "701177",
            "state": "active",
            "status_label": "activo",
            "partner_name": "Nancy Mercedes",
            "residual": "0.00",
        }
    ]
    contact_state["selected_contract"] = "701177"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "support"
    contact_state["support"] = {
        "awaiting_otp": False,
        "awaiting_credentials": False,
        "awaiting_issue_type": False,
        "awaiting_resolution_confirmation": True,
        "last_issue_type": "slow_internet",
        "last_system_issue": "monitoreo previo",
        "manual_checks_requested": False,
        "manual_checks_confirmed": False,
        "pending_contract": None,
    }

    result = asyncio.run(
        service.handle(
            preferred_domain="support",
            message=InboundMessage(
                mensaje="la verdad sigue molestando",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert result.intent == "human_handoff"
    assert handoffs
    assert "701177" in (handoffs[0] or "")


def test_contact_flow_escalates_after_second_duplicate_proof(monkeypatch):
    handoffs = []
    relayed = []

    async def fake_analyze(self, attachment, *, notify_gonet_bot: bool = False):
        return {
            "estado": "validated",
            "debe_reintentar": False,
            "texto_extraido": "Comprobante válido",
        }

    async def fake_register(*, contract, ocr_result, attachments, cedula):
        return {
            "status": "duplicate",
            "resolved": {
                "code": "130283",
                "value": 18.00,
                "pending_value": 22.89,
                "deposit": {"name": "BANCO PICHINCHA"},
            },
        }

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
        return {"status": "sent", "internal_user": 15, "channel_id": 44}

    async def fake_relay_attachments(
        *,
        channel,
        recipient,
        attachments,
        cedula=None,
        origen=None,
        internal_user=None,
        channel_id=None,
    ):
        relayed.append(
            {
                "channel": channel,
                "recipient": recipient,
                "attachments": attachments,
                "cedula": cedula,
                "origen": origen,
                "internal_user": internal_user,
                "channel_id": channel_id,
            }
        )
        return {"status": "sent"}

    service = ContactFlowService()
    monkeypatch.setattr("packages.integrations.ocr_service_client.OCRServiceClient.enabled", property(lambda self: True))
    monkeypatch.setattr("packages.integrations.ocr_service_client.OCRServiceClient.analyze", fake_analyze)
    monkeypatch.setattr(service.billing, "register_payment", fake_register)
    monkeypatch.setattr(service.handoff, "escalate_new_client", fake_handoff)
    monkeypatch.setattr(service.handoff, "relay_attachments", fake_relay_attachments)

    state = SessionState(session_id="contact-flow-duplicate-proof-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "800003",
            "state": "cortado",
            "status_label": "cortado",
            "partner_name": "Nancy Mercedes",
            "residual": "22.89",
        }
    ]
    contact_state["selected_contract"] = "800003"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "billing"
    contact_state["billing"] = {
        "awaiting_action": False,
        "awaiting_proof": True,
        "proof_attempts": 1,
        "proof_failures": ["Intento 1: el pago ya estaba registrado y el comprobante no es válido como nuevo pago"],
        "processing_async": False,
        "pending_proof_attachments": [],
    }

    result = asyncio.run(
        service.handle(
            preferred_domain="billing",
            message=InboundMessage(
                mensaje="Adjunto comprobante",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
                attachments=[
                    {
                        "filename": "proof.png",
                        "mime_type": "image/png",
                        "base64_data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
                    }
                ],
            ),
            state=state,
        )
    )

    assert result.intent == "billing_handoff"
    assert "asesor especializado" in result.message.lower()
    assert len(handoffs) == 1
    assert len(relayed) == 1
    assert relayed[0]["attachments"]
    assert relayed[0]["attachments"][0]["filename"] == "proof.png"
    billing_state = (state.metadata.get("contact") or {}).get("billing") or {}
    assert billing_state.get("proof_attempts") == 0


def test_contact_flow_support_slow_internet_uses_diagnostic_guidance(monkeypatch):
    async def fake_monitor(self, contrato: str):
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 2}}}

    async def fake_connected(self, contrato: str):
        return {"ok": True, "data": {"count": 12, "devices": [{}] * 12}}

    async def fake_onu_status(self, contrato: str):
        return {"ok": True, "status": "working", "power_dbm": -20.22}

    service = ContactFlowService()
    monkeypatch.setattr(service.smart, "enabled", lambda: True)
    monkeypatch.setattr(service.smart, "monitor_contract", fake_monitor.__get__(service.smart, type(service.smart)))
    monkeypatch.setattr(service.smart, "connected_devices_for_contract", fake_connected.__get__(service.smart, type(service.smart)))
    monkeypatch.setattr(service.onu, "enabled", lambda: True)
    monkeypatch.setattr(service.onu, "get_status", fake_onu_status.__get__(service.onu, type(service.onu)))

    state = SessionState(session_id="contact-flow-support-guidance-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "500007",
            "state": "active",
            "status_label": "activo",
            "partner_name": "Christian Montero",
        }
    ]
    contact_state["selected_contract"] = "500007"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "support"

    result = asyncio.run(
        service.handle(
            preferred_domain="support",
            message=InboundMessage(
                mensaje="esta malo y lento no sirve",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    support_state = (state.metadata.get("contact") or {}).get("support") or {}
    lowered = result.message.lower()
    assert result.intent == "support_network_monitoring"
    assert "*12*" in result.message
    assert "carga en el wifi" in lowered or "equipos conectados" in lowered
    assert "un solo equipo" in lowered
    assert support_state.get("awaiting_resolution_confirmation") is True
    assert support_state.get("last_followup_prompt")
    assert (support_state.get("last_response_plan") or {}).get("hypothesis") == "wifi_load"
    assert (result.metadata.get("response_plan") or {}).get("next_step") == "reduce_load_and_isolate_device"


def test_contact_flow_support_slow_internet_keeps_backend_snapshot_in_diagnostic_context(monkeypatch):
    async def fake_monitor(self, contrato: str):
        return {
            "ok": True,
            "data": {
                "accion": "monitoreo",
                "info": {
                    "status": "200 OK",
                    "message": "Dispositivo encontrado",
                    "data": {
                        "iden": 9199,
                        "serverId": "00E0FC-Huawei-QDU7S23C07000898",
                        "conexion": "2026-04-09T22:08:48.274Z",
                        "numeroRedes": "2",
                        "modelo": {
                            "nombre": "AX3",
                            "marca": {"nombre": "HUAWEI"},
                        },
                        "planDevice": {
                            "nombre": "PLAN GOPLUS",
                            "cantidad": 350,
                        },
                    },
                },
            },
        }

    async def fake_connected(self, contrato: str):
        return {
            "ok": True,
            "data": {
                "count": 6,
                "devices": [
                    {"Activo": True, "bandR": {"NombreRed": "LAN-1"}},
                    {"Activo": True, "bandR": {"NombreRed": "LAN-1"}},
                    {"Activo": True, "bandR": {"NombreRed": "Mesh"}},
                    {"Activo": True, "bandR": {"NombreRed": "Casa 5G"}},
                    {"Activo": True, "bandR": {"NombreRed": "Casa 2.4G"}},
                    {"Activo": True, "bandR": {}},
                ],
            },
        }

    async def fake_onu_status(self, contrato: str):
        return {"ok": True, "status": "working", "power_dbm": -20.22}

    service = ContactFlowService()
    monkeypatch.setattr(service.smart, "enabled", lambda: True)
    monkeypatch.setattr(service.smart, "monitor_contract", fake_monitor.__get__(service.smart, type(service.smart)))
    monkeypatch.setattr(service.smart, "connected_devices_for_contract", fake_connected.__get__(service.smart, type(service.smart)))
    monkeypatch.setattr(service.onu, "enabled", lambda: True)
    monkeypatch.setattr(service.onu, "get_status", fake_onu_status.__get__(service.onu, type(service.onu)))

    state = SessionState(session_id="contact-flow-support-tr069-snapshot-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "500007",
            "state": "active",
            "status_label": "activo",
            "partner_name": "Christian Montero",
        }
    ]
    contact_state["selected_contract"] = "500007"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "support"

    result = asyncio.run(
        service.handle(
            preferred_domain="support",
            message=InboundMessage(
                mensaje="internet lento",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    support_state = (state.metadata.get("contact") or {}).get("support") or {}
    diagnostic = support_state.get("last_diagnostic") or {}
    lowered = result.message.lower()
    assert result.intent == "support_network_monitoring"
    assert diagnostic.get("plan_name") == "PLAN GOPLUS"
    assert diagnostic.get("plan_speed_mbps") == 350
    assert diagnostic.get("device_model") == "HUAWEI AX3"
    assert diagnostic.get("network_count") == 2
    assert diagnostic.get("lan_devices") == 2
    assert diagnostic.get("mesh_devices") == 1
    assert diagnostic.get("wifi_24g_devices") == 1
    assert diagnostic.get("wifi_5g_devices") == 1
    assert "350 mbps" not in lowered
    assert "mesh" not in lowered
    assert "lan" not in lowered


def test_contact_flow_support_no_service_persists_after_manual_check_and_goes_to_handoff(monkeypatch):
    handoffs = []

    async def fake_handoff(*, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(summary or "")
        return {"status": "sent"}

    service = ContactFlowService()
    monkeypatch.setattr(service.handoff, "escalate_new_client", fake_handoff)

    state = SessionState(session_id="contact-flow-support-persisting-no-service-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "500007",
            "state": "active",
            "status_label": "activo",
            "partner_name": "Christian Montero",
        }
    ]
    contact_state["selected_contract"] = "500007"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "support"
    contact_state["support"] = {
        "awaiting_otp": False,
        "awaiting_credentials": False,
        "awaiting_issue_type": False,
        "awaiting_resolution_confirmation": True,
        "last_issue_type": "no_service",
        "last_system_issue": "monitoreo previo",
        "last_diagnostic": {"issue_type": "no_service"},
        "last_response_plan": None,
        "last_followup_prompt": "Si ya lo revisó y sigue sin internet, indíqueme y lo derivo con un asesor especializado.",
        "guided_followup_attempts": 0,
        "manual_checks_requested": True,
        "manual_checks_confirmed": False,
        "pending_contract": None,
    }

    result = asyncio.run(
        service.handle(
            preferred_domain="support",
            message=InboundMessage(
                mensaje="ya revise y no sirve esa huevada",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert result.intent == "human_handoff"
    assert handoffs
    assert "500007" in handoffs[0]
    assert "cuando lo revise" not in result.message.lower()


def test_contact_flow_support_repeated_no_service_short_circuits_to_handoff(monkeypatch):
    handoffs = []

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

    service = ContactFlowService()
    monkeypatch.setattr(service.handoff, "escalate_new_client", fake_handoff)

    state = SessionState(session_id="contact-flow-support-repeat-no-service-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "500007",
            "state": "active",
            "status_label": "activo",
            "partner_name": "Christian Montero",
        }
    ]
    contact_state["selected_contract"] = "500007"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "support"
    contact_state["support"] = {
        "awaiting_otp": False,
        "awaiting_credentials": False,
        "awaiting_issue_type": False,
        "awaiting_resolution_confirmation": False,
        "last_issue_type": "no_service",
        "last_system_issue": "monitoreo previo",
        "last_diagnostic": {"issue_type": "no_service", "contract_code": "500007"},
        "last_response_plan": None,
        "last_followup_prompt": "No veo una caída base en la red. Revise una sola vez que la ONU y el router estén encendidos y que los cables estén bien conectados.",
        "guided_followup_attempts": 0,
        "manual_checks_requested": False,
        "manual_checks_confirmed": False,
        "pending_contract": None,
    }

    result = asyncio.run(
        service.handle(
            preferred_domain="support",
            message=InboundMessage(
                mensaje="no tengo internet",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert result.intent == "human_handoff"
    assert handoffs
    assert "500007" in handoffs[0]["summary"]


def test_contact_flow_support_plain_negative_after_followup_short_circuits_to_handoff(monkeypatch):
    handoffs = []

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

    service = ContactFlowService()
    monkeypatch.setattr(service.handoff, "escalate_new_client", fake_handoff)

    state = SessionState(session_id="contact-flow-support-plain-negative-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "500007",
            "state": "active",
            "status_label": "activo",
            "partner_name": "Christian Montero",
        }
    ]
    contact_state["selected_contract"] = "500007"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "support"
    contact_state["support"] = {
        "awaiting_otp": False,
        "awaiting_credentials": False,
        "awaiting_issue_type": False,
        "awaiting_resolution_confirmation": True,
        "last_issue_type": "no_service",
        "last_system_issue": "monitoreo previo",
        "last_diagnostic": {"issue_type": "no_service", "contract_code": "500007"},
        "last_response_plan": None,
        "last_followup_prompt": "No veo una caída base en la red. Revise una sola vez que la ONU y el router estén encendidos y que los cables estén bien conectados.",
        "guided_followup_attempts": 0,
        "manual_checks_requested": True,
        "manual_checks_confirmed": False,
        "pending_contract": None,
    }

    result = asyncio.run(
        service.handle(
            preferred_domain="support",
            message=InboundMessage(
                mensaje="no sirve",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert result.intent == "human_handoff"
    assert handoffs
    assert "500007" in handoffs[0]["summary"]


def test_contact_flow_support_no_service_after_monitoring_goes_to_handoff(monkeypatch):
    handoffs = []

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

    service = ContactFlowService()
    monkeypatch.setattr(service.handoff, "escalate_new_client", fake_handoff)

    state = SessionState(session_id="contact-flow-support-after-monitoring-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "500007",
            "state": "active",
            "status_label": "activo",
            "partner_name": "Christian Montero",
        }
    ]
    contact_state["selected_contract"] = "500007"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "support"
    contact_state["support"] = {
        "awaiting_otp": False,
        "awaiting_credentials": False,
        "awaiting_issue_type": False,
        "awaiting_resolution_confirmation": True,
        "last_issue_type": "no_service",
        "last_system_issue": "monitoreo previo",
        "last_diagnostic": {"issue_type": "no_service", "contract_code": "500007"},
        "last_response_plan": None,
        "last_followup_prompt": "No veo una caída base en la red. Revise una sola vez que la ONU y el router estén encendidos y que los cables estén bien conectados.",
        "guided_followup_attempts": 0,
        "manual_checks_requested": False,
        "manual_checks_confirmed": False,
        "pending_contract": None,
    }

    result = asyncio.run(
        service.handle(
            preferred_domain="support",
            message=InboundMessage(
                mensaje="no sirve",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert result.intent == "human_handoff"
    assert handoffs
    assert "500007" in handoffs[0]["summary"]


def test_contact_flow_support_handoff_timeout_still_returns_human_handoff(monkeypatch):
    async def fake_handoff(*, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        raise httpx.ReadTimeout("timeout", request=httpx.Request("POST", "https://example.com"))

    service = ContactFlowService()
    monkeypatch.setattr(service.handoff, "escalate_new_client", fake_handoff)

    state = SessionState(session_id="contact-flow-support-timeout-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "500007",
            "state": "active",
            "status_label": "activo",
            "partner_name": "Christian Montero",
        }
    ]
    contact_state["selected_contract"] = "500007"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "support"
    contact_state["support"] = {
        "awaiting_otp": False,
        "awaiting_credentials": False,
        "awaiting_issue_type": False,
        "awaiting_resolution_confirmation": True,
        "last_issue_type": "no_service",
        "last_system_issue": "monitoreo previo",
        "last_diagnostic": {"issue_type": "no_service", "contract_code": "500007"},
        "last_response_plan": None,
        "last_followup_prompt": "No veo una caída base en la red. Revise una sola vez que la ONU y el router estén encendidos y que los cables estén bien conectados.",
        "guided_followup_attempts": 0,
        "manual_checks_requested": False,
        "manual_checks_confirmed": False,
        "pending_contract": None,
    }

    result = asyncio.run(
        service.handle(
            preferred_domain="support",
            message=InboundMessage(
                mensaje="ya revise y no sirve esa huevada",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    assert result.intent == "human_handoff"
    assert result.metadata.get("handoff_timeout") is True
    assert "vuelva a escribirme" not in result.message.lower()


def test_contact_flow_support_followup_turns_into_targeted_guidance(monkeypatch):
    service = ContactFlowService()
    state = SessionState(session_id="contact-flow-support-guidance-followup-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "500007",
            "state": "active",
            "status_label": "activo",
            "partner_name": "Christian Montero",
        }
    ]
    contact_state["selected_contract"] = "500007"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "support"
    contact_state["support"] = {
        "awaiting_otp": False,
        "awaiting_credentials": False,
        "awaiting_issue_type": False,
        "awaiting_resolution_confirmation": True,
        "last_issue_type": "slow_internet",
        "last_system_issue": None,
        "last_diagnostic": {
            "issue_type": "slow_internet",
            "contract_code": "500007",
            "connected_devices": 12,
            "onu_status": "working",
            "power_dbm": -20.22,
            "rebooted": False,
            "proactive_reboot": False,
            "message": "diagnostico previo",
            "followup_prompt": "Indíqueme si pasa en todos o solo en uno.",
            "diagnostic_style": "wifi_load_high",
        },
        "last_followup_prompt": "Indíqueme si pasa en todos o solo en uno.",
        "guided_followup_attempts": 0,
        "manual_checks_requested": False,
        "manual_checks_confirmed": False,
        "pending_contract": None,
    }

    result = asyncio.run(
        service.handle(
            preferred_domain="support",
            message=InboundMessage(
                mensaje="solo en el televisor",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    support_state = (state.metadata.get("contact") or {}).get("support") or {}
    lowered = result.message.lower()
    assert result.intent == "support_network_followup"
    assert "un solo equipo" in lowered or "televisor" in lowered
    assert "olvidar y reconectar" in lowered or "5g" in lowered or "por cable" in lowered
    assert support_state.get("guided_followup_attempts") == 1
    assert support_state.get("awaiting_resolution_confirmation") is True
    assert (support_state.get("last_response_plan") or {}).get("hypothesis") == "single_device_issue"


def test_contact_flow_support_followup_keeps_waiting_on_acknowledgement():
    service = ContactFlowService()
    state = SessionState(session_id="contact-flow-support-ack-followup-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "500007",
            "state": "active",
            "status_label": "activo",
            "partner_name": "Christian Montero",
        }
    ]
    contact_state["selected_contract"] = "500007"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "support"
    contact_state["support"] = {
        "awaiting_otp": False,
        "awaiting_credentials": False,
        "awaiting_issue_type": False,
        "awaiting_resolution_confirmation": True,
        "last_issue_type": "slow_internet",
        "last_system_issue": None,
        "last_diagnostic": {
            "issue_type": "slow_internet",
            "contract_code": "500007",
            "connected_devices": 8,
            "onu_status": "working",
            "power_dbm": -20.22,
            "rebooted": False,
            "proactive_reboot": False,
            "message": "diagnostico previo",
            "followup_prompt": "Indíqueme si ya quedó bien o si todavía sigue igual.",
            "diagnostic_style": "wifi_load_high",
        },
        "last_followup_prompt": "Indíqueme si ya quedó bien o si todavía sigue igual.",
        "guided_followup_attempts": 0,
        "manual_checks_requested": False,
        "manual_checks_confirmed": False,
        "pending_contract": None,
    }

    result = asyncio.run(
        service.handle(
            preferred_domain="support",
            message=InboundMessage(
                mensaje="ok",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    support_state = (state.metadata.get("contact") or {}).get("support") or {}
    assert result.intent == "support_network_followup"
    assert "todavía sigue igual" in result.message.lower() or "todavia sigue igual" in result.message.lower()
    assert support_state.get("awaiting_resolution_confirmation") is True


def test_contact_flow_support_edit_network_wrong_otp_uses_human_retry_copy():
    service = ContactFlowService()
    state = SessionState(session_id="contact-flow-support-otp-retry-1", cedula="0102030405")
    contact_state = service._state(state)
    contact_state["contracts"] = [
        {
            "code": "500007",
            "state": "active",
            "status_label": "activo",
            "partner_name": "Christian Montero",
        }
    ]
    contact_state["selected_contract"] = "500007"
    contact_state["consent_accepted"] = True
    contact_state["last_domain"] = "support"
    contact_state["support"] = {
        "awaiting_otp": True,
        "awaiting_credentials": False,
        "awaiting_issue_type": False,
        "awaiting_resolution_confirmation": False,
        "last_issue_type": "edit_network",
        "last_system_issue": None,
        "last_diagnostic": None,
        "last_response_plan": None,
        "last_followup_prompt": None,
        "guided_followup_attempts": 0,
        "manual_checks_requested": False,
        "manual_checks_confirmed": False,
        "pending_contract": "500007",
    }

    async def fake_verify(recipient, session_id, code):
        return {"ok": True, "data": {"verified": False, "locked": False, "attempts_left": 2}}

    service.otp.verify_otp = fake_verify

    result = asyncio.run(
        service.handle(
            preferred_domain="support",
            message=InboundMessage(
                mensaje="123456",
                channel="whatsapp",
                recipient="593111",
                session_id=state.session_id,
                cedula="0102030405",
            ),
            state=state,
        )
    )

    support_state = (state.metadata.get("contact") or {}).get("support") or {}
    assert result.intent == "support_edit_network_otp_retry"
    assert "le quedan 2 intentos" in result.message.lower()
    assert (result.metadata.get("response_plan") or {}).get("hypothesis") == "otp_incorrect"
    assert (support_state.get("last_response_plan") or {}).get("conversation_state") == "edit_network_otp_retry"
