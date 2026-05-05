import asyncio

from packages.integrations.redis_store import build_session_store
from packages.shared.config import get_settings
from packages.agents.contact_support_utils import classify_support_issue, is_negative
from packages.orchestrator.service import OrchestratorService
from packages.orchestrator.session_context import SessionContextService
from packages.shared.schemas import AgentResult, Attachment, InboundMessage, RouteDecision, SessionState


def test_support_issue_detects_internet_no_sirve_as_no_service():
    assert classify_support_issue("El internet no sirve") == "no_service"


def test_support_issue_tolerates_typos_for_no_service_signal():
    assert classify_support_issue("El internte no sirbe") == "no_service"


def test_support_issue_detects_manual_check_persistence_as_no_service():
    assert classify_support_issue("ya revise y no sirve esa huevada") == "no_service"
    assert classify_support_issue("ya revise y no vale") == "no_service"
    assert is_negative("ya revise y no sirve esa huevada") is True


def test_support_issue_detects_no_vale_as_no_service():
    assert classify_support_issue("mi internet no vale") == "no_service"


def test_orchestrator_detects_context_free_missing_identity_reply():
    service = OrchestratorService()
    assert service._user_reports_missing_identity("No sé") is True
    assert service._user_reports_missing_identity("Nose") is True
    assert service._user_reports_missing_identity("No la se") is True
    assert service._user_reports_missing_identity("No recuerdo") is True


def test_orchestrator_keeps_missing_identity_lane_for_nose(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    build_session_store.cache_clear()

    async def noop(**kwargs):
        return {"status": "ok"}

    async def run_flow():
        service = OrchestratorService()
        monkeypatch.setattr(service.contact_registry, "touch_contact", noop)
        monkeypatch.setattr(service.contact_registry, "mark_ai_active", noop)
        monkeypatch.setattr(service.contact_registry, "mark_human_active", noop)
        monkeypatch.setattr(service.handoff.odoo, "escalate_new_client", noop)

        first = await service.handle_message(
            InboundMessage(
                mensaje="El internet no sirve",
                channel="whatsapp",
                recipient="593999",
                session_id="nose-flow-1",
            )
        )
        second = await service.handle_message(
            InboundMessage(
                mensaje="No se",
                channel="whatsapp",
                recipient="593999",
                session_id="nose-flow-1",
            )
        )
        return first, second

    first, second = asyncio.run(run_flow())
    assert first.intent == "ask_cedula"
    assert second.intent == "human_handoff"
    assert "dame un momento" in second.message.lower()
    assert "asesor especializado" in second.message.lower()


def test_orchestrator_deduplicates_repeated_inbound_message_id(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    build_session_store.cache_clear()

    calls = []

    async def noop(**kwargs):
        return {"status": "ok"}

    async def fake_decide(message, state):
        return RouteDecision(
            agent="sales",
            intent="sales",
            confidence=0.9,
            reason="test_route",
        )

    async def fake_execute(decision, message, state):
        calls.append((message.mensaje, (message.metadata or {}).get("message_id")))
        return AgentResult(
            message="respuesta demo",
            intent="sales",
            agent="sales",
        )

    async def run_flow():
        service = OrchestratorService()
        monkeypatch.setattr(service.contact_registry, "touch_contact", noop)
        monkeypatch.setattr(service.contact_registry, "mark_ai_active", noop)
        monkeypatch.setattr(service.contact_registry, "mark_human_active", noop)
        monkeypatch.setattr(service.router, "decide", fake_decide)
        monkeypatch.setattr(service, "_execute", fake_execute)

        first = await service.handle_message(
            InboundMessage(
                mensaje="planes",
                channel="whatsapp",
                recipient="593999",
                session_id="dedupe-1",
                metadata={"message_id": "wamid-1"},
            )
        )
        second = await service.handle_message(
            InboundMessage(
                mensaje="planes",
                channel="whatsapp",
                recipient="593999",
                session_id="dedupe-1",
                metadata={"message_id": "wamid-1"},
            )
        )
        return first, second

    first, second = asyncio.run(run_flow())
    assert "respuesta demo" in first.message
    assert second.metadata.get("skip_delivery") is True
    assert calls == [("planes", "wamid-1")]


def test_orchestrator_clarifies_when_user_sends_holder_name_instead_of_document(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    build_session_store.cache_clear()

    async def noop(**kwargs):
        return {"status": "ok"}

    async def run_flow():
        service = OrchestratorService()
        monkeypatch.setattr(service.contact_registry, "touch_contact", noop)
        monkeypatch.setattr(service.contact_registry, "mark_ai_active", noop)
        monkeypatch.setattr(service.contact_registry, "mark_human_active", noop)

        first = await service.handle_message(
            InboundMessage(
                mensaje="No sirve el internet",
                channel="whatsapp",
                recipient="593999",
                session_id="holder-name-1",
            )
        )
        second = await service.handle_message(
            InboundMessage(
                mensaje="Bryan André Macas Cordero",
                channel="whatsapp",
                recipient="593999",
                session_id="holder-name-1",
            )
        )
        return first, second

    first, second = asyncio.run(run_flow())
    assert first.intent == "ask_cedula"
    assert second.intent == "ask_cedula"
    assert "ese es el nombre del titular" in second.message.lower()
    assert "cedula o ruc" in second.message.lower() or "cédula o ruc" in second.message.lower()


def test_orchestrator_does_not_confuse_typoed_sales_discovery_with_holder_name():
    service = OrchestratorService()
    assert service._user_sent_holder_name_instead_of_document("queee intrsnet ofreces?") is False


def test_orchestrator_greeting_copy_is_less_menu_like():
    service = OrchestratorService()
    result = service._build_clarify_result("greeting_only", user_message="hola")

    assert "le atiende" in result.message.lower()
    assert "si quieres" not in result.message.lower()
    assert "¿en qué puedo ayudarle" in result.message.lower()


def test_orchestrator_small_talk_reply_feels_human_but_redirects():
    service = OrchestratorService()
    result = service._build_clarify_result("small_talk", user_message="Que tal tu día?")

    assert "todo bien por aquí" in result.message.lower()
    assert "internet, pagos o planes" in result.message.lower()


def test_orchestrator_caches_first_payment_proof_image_before_identity(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    build_session_store.cache_clear()

    async def noop(**kwargs):
        return {"status": "ok"}

    async def fake_decide(message, state):
        return RouteDecision(
            agent="billing",
            intent="billing",
            confidence=0.92,
            reason="attachment_payment_proof",
        )

    async def run_flow():
        service = OrchestratorService()
        monkeypatch.setattr(service.contact_registry, "touch_contact", noop)
        monkeypatch.setattr(service.contact_registry, "mark_ai_active", noop)
        monkeypatch.setattr(service.contact_registry, "mark_human_active", noop)
        monkeypatch.setattr(service.router, "decide", fake_decide)

        response = await service.handle_message(
            InboundMessage(
                mensaje="Imagen enviada",
                channel="whatsapp",
                recipient="593999",
                session_id="proof-first-1",
                attachments=[
                    Attachment(
                        type="image",
                        mime_type="image/jpeg",
                        filename="proof.jpg",
                        base64_data="ZmFrZQ==",
                    )
                ],
            )
        )
        state = await SessionContextService().load(
            InboundMessage(
                mensaje="",
                channel="whatsapp",
                recipient="593999",
                session_id="proof-first-1",
            )
        )
        return response, state

    response, state = asyncio.run(run_flow())
    billing_state = (((state.metadata or {}).get("contact") or {}).get("billing") or {})

    assert response.intent == "ask_cedula"
    assert "parece un comprobante de pago" in response.message.lower()
    assert billing_state.get("pending_proof_attachments")


def test_orchestrator_hydrates_identity_from_valid_document_text_even_with_image_attachment(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    build_session_store.cache_clear()

    captured = {}

    async def noop(**kwargs):
        return {"status": "ok"}

    async def fake_decide(message, state):
        return RouteDecision(
            agent="billing",
            intent="billing",
            confidence=0.92,
            reason="attachment_payment_proof",
        )

    async def fake_billing_handle(message, state):
        captured["cedula"] = message.cedula
        captured["state_cedula"] = state.cedula
        return AgentResult(
            message="respuesta demo",
            intent="billing",
            agent="billing",
        )

    async def run_flow():
        service = OrchestratorService()
        monkeypatch.setattr(service.contact_registry, "touch_contact", noop)
        monkeypatch.setattr(service.contact_registry, "mark_ai_active", noop)
        monkeypatch.setattr(service.contact_registry, "mark_human_active", noop)
        monkeypatch.setattr(service.router, "decide", fake_decide)
        monkeypatch.setattr(service.billing, "handle", fake_billing_handle)

        response = await service.handle_message(
            InboundMessage(
                mensaje="0702408915",
                channel="whatsapp",
                recipient="593999",
                session_id="identity-hydrate-1",
                attachments=[
                    Attachment(
                        type="image",
                        mime_type="image/jpeg",
                        filename="cedula.jpg",
                        base64_data="ZmFrZQ==",
                    )
                ],
            )
        )
        return response

    response = asyncio.run(run_flow())

    assert response.intent == "billing"
    assert captured["cedula"] == "0702408915"
    assert captured["state_cedula"] == "0702408915"


def test_orchestrator_identity_helpers_attach_response_plan():
    async def run_check():
        service = OrchestratorService()
        state = SessionState(session_id="clarify-plan-1")

        async def fake_handoff(**kwargs):
            return {"status": "ok"}

        service.handoff.odoo.escalate_new_client = fake_handoff

        proof = service._build_billing_proof_identity_result(state=state, pending_message="Adjunto comprobante")
        missing = await service._build_missing_identity_result(
            message=InboundMessage(
                mensaje="No sé",
                channel="whatsapp",
                recipient="593999",
                session_id=state.session_id,
            ),
            state=state,
            pending_agent="support",
            pending_message="no sirve",
        )
        name_only = service._build_identity_name_clarification_result(pending_agent="billing", pending_message="Juan Perez")
        generic = service._build_clarify_result("out_of_scope", state=state, user_message="quiero saber del clima")
        return proof, missing, name_only, generic

    proof, missing, name_only, generic = asyncio.run(run_check())

    assert proof.metadata["response_plan"]["conversation_state"] == "billing_proof_identity_request"
    assert missing.metadata["response_plan"]["conversation_state"] == "handoff_created"
    assert name_only.metadata["response_plan"]["conversation_state"] == "identity_name_instead_of_document"
    assert generic.metadata["response_plan"]["conversation_state"] == "clarify_out_of_scope"


def test_orchestrator_preserves_original_media_url_when_caching_payment_proof():
    service = OrchestratorService()
    state = SessionState(session_id="proof-original-url-1")

    service._cache_pending_billing_proof_attachments(
        state=state,
        attachments=[
            Attachment(
                type="image",
                mime_type="image/jpeg",
                filename="proof.jpg",
                url="https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123",
                base64_data="ZmFrZQ==",
            )
        ],
    )

    billing_state = (((state.metadata or {}).get("contact") or {}).get("billing") or {})
    cached = billing_state.get("pending_proof_attachments") or []
    assert cached
    assert cached[0]["url"] == "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123"
