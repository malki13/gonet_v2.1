import asyncio

from packages.orchestrator.conversation_classifier import ConversationClassifier
from packages.shared.config import get_settings
from packages.shared.schemas import InboundMessage, SessionState


def test_conversation_classifier_keeps_payment_priority_over_commercial_keywords():
    classifier = ConversationClassifier()

    result = asyncio.run(
        classifier.classify(
            message=InboundMessage(
                mensaje="Quiero registrar un pago del plan de internet",
                channel="whatsapp",
                recipient="593999",
                session_id="cc-1",
            ),
            state=SessionState(session_id="cc-1"),
        )
    )

    assert result["conversation_mode"] == "task"
    assert result["business_domain"] == "billing"
    assert result["reason"] == "payment_keywords"


def test_conversation_classifier_routes_promotions_to_sales():
    classifier = ConversationClassifier()

    result = asyncio.run(
        classifier.classify(
            message=InboundMessage(
                mensaje="q promociones tienes?",
                channel="whatsapp",
                recipient="593999",
                session_id="cc-sales-promo-1",
            ),
            state=SessionState(session_id="cc-sales-promo-1"),
        )
    )

    assert result["conversation_mode"] == "task"
    assert result["business_domain"] == "sales"
    assert result["reason"] == "sales_intent_plan"


def test_conversation_classifier_routes_promos_shorthand_to_sales():
    classifier = ConversationClassifier()

    result = asyncio.run(
        classifier.classify(
            message=InboundMessage(
                mensaje="q promos tienes?",
                channel="whatsapp",
                recipient="593999",
                session_id="cc-sales-promos-1",
            ),
            state=SessionState(session_id="cc-sales-promos-1"),
        )
    )

    assert result["conversation_mode"] == "task"
    assert result["business_domain"] == "sales"
    assert result["reason"] == "sales_intent_plan"


def test_conversation_classifier_routes_typoed_catalog_question_to_sales():
    classifier = ConversationClassifier()

    result = asyncio.run(
        classifier.classify(
            message=InboundMessage(
                mensaje="q proms tinees?",
                channel="whatsapp",
                recipient="593999",
                session_id="cc-sales-typo-1",
            ),
            state=SessionState(session_id="cc-sales-typo-1"),
        )
    )

    assert result["conversation_mode"] == "task"
    assert result["business_domain"] == "sales"
    assert result["reason"] == "sales_intent_plan"


def test_conversation_classifier_routes_que_internets_ofrecen_to_sales():
    classifier = ConversationClassifier()

    result = asyncio.run(
        classifier.classify(
            message=InboundMessage(
                mensaje="hola q internets ofrecen?",
                channel="whatsapp",
                recipient="593999",
                session_id="cc-sales-internets-1",
            ),
            state=SessionState(session_id="cc-sales-internets-1"),
        )
    )

    assert result["conversation_mode"] == "task"
    assert result["business_domain"] == "sales"
    assert result["reason"] == "commercial_discovery_keywords"


def test_conversation_classifier_keeps_support_priority_for_ambiguous_internet_issue():
    classifier = ConversationClassifier()

    result = asyncio.run(
        classifier.classify(
            message=InboundMessage(
                mensaje="internet lento",
                channel="whatsapp",
                recipient="593999",
                session_id="cc-support-1",
            ),
            state=SessionState(session_id="cc-support-1"),
        )
    )

    assert result["conversation_mode"] == "task"
    assert result["business_domain"] == "support"
    assert result["reason"] == "support_keywords"


def test_conversation_classifier_accepts_valid_openai_semantic_result(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_classifier_enabled", True)
    monkeypatch.setattr(settings, "conversation_classifier_mode", "openai")

    class FakeLLM:
        async def classify_conversation(self, **kwargs):
            return {
                "status": "ok",
                "result": {
                    "conversation_mode": "small_talk",
                    "business_domain": "none",
                    "confidence": 0.77,
                    "reason": "social_question",
                },
            }

    classifier = ConversationClassifier(llm=FakeLLM())
    result = asyncio.run(
        classifier.classify(
            message=InboundMessage(
                mensaje="¿Qué tal tu día?",
                channel="whatsapp",
                recipient="593999",
                session_id="cc-2",
            ),
            state=SessionState(session_id="cc-2"),
        )
    )

    assert result["conversation_mode"] == "small_talk"
    assert result["business_domain"] == "none"
    assert result["reason"] == "small_talk"
    assert result["source"] == "openai"


def test_conversation_classifier_falls_back_to_heuristic_on_invalid_openai_result(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_classifier_enabled", True)
    monkeypatch.setattr(settings, "conversation_classifier_mode", "openai")

    class FakeLLM:
        async def classify_conversation(self, **kwargs):
            return {
                "status": "ok",
                "result": {
                    "conversation_mode": "task",
                    "business_domain": "none",
                    "confidence": 0.99,
                    "reason": "invented_domain",
                },
            }

    classifier = ConversationClassifier(llm=FakeLLM())
    result = asyncio.run(
        classifier.classify(
            message=InboundMessage(
                mensaje="hola",
                channel="whatsapp",
                recipient="593999",
                session_id="cc-3",
            ),
            state=SessionState(session_id="cc-3"),
        )
    )

    assert result["conversation_mode"] == "greeting_only"
    assert result["business_domain"] == "none"
    assert result["source"] == "heuristic"


def test_conversation_classifier_keeps_strong_sales_heuristic_when_openai_downgrades_to_greeting(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_classifier_enabled", True)
    monkeypatch.setattr(settings, "conversation_classifier_mode", "openai")

    class FakeLLM:
        async def classify_conversation(self, **kwargs):
            return {
                "status": "ok",
                "result": {
                    "conversation_mode": "greeting_only",
                    "business_domain": "none",
                    "confidence": 0.91,
                    "reason": "greeting_only",
                },
            }

    classifier = ConversationClassifier(llm=FakeLLM())
    result = asyncio.run(
        classifier.classify(
            message=InboundMessage(
                mensaje="hola q planes tinen?",
                channel="whatsapp",
                recipient="593999",
                session_id="cc-sales-protected-1",
            ),
            state=SessionState(session_id="cc-sales-protected-1"),
        )
    )

    assert result["conversation_mode"] == "task"
    assert result["business_domain"] == "sales"
    assert result["reason"] == "sales_intent_plan"
    assert result["source"] == "heuristic"


def test_conversation_classifier_uses_contextual_route_after_clarify(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_classifier_enabled", True)
    monkeypatch.setattr(settings, "conversation_classifier_mode", "openai")

    class FakeLLM:
        def enabled(self):
            return True

        async def extract_json(self, **kwargs):
            payload = (kwargs or {}).get("payload") or {}
            flow_context = payload.get("flow_context") or {}
            message = (payload.get("message") or {}).get("normalized")
            if flow_context.get("flow_name") == "orchestrator_routing" and message == "quiero ver cuanto debo":
                return {
                    "status": "ok",
                    "result": {
                        "action": "switch_intent",
                        "target_intent": "billing",
                        "confidence": 0.93,
                        "reason": "billing_in_context",
                        "slot_updates": {},
                    },
                }
            return {"status": "ok", "result": {}}

        async def classify_conversation(self, **kwargs):
            raise AssertionError("contextual route should short-circuit before classify_conversation")

    classifier = ConversationClassifier(llm=FakeLLM())
    state = SessionState(session_id="cc-contextual-route-1", current_intent="clarify", last_agent="clarify")
    state.history.extend(
        [
            {"role": "assistant", "content": "Indíqueme si su consulta es por soporte técnico, facturación o planes."},
            {"role": "user", "content": "hola"},
        ]
    )

    result = asyncio.run(
        classifier.classify(
            message=InboundMessage(
                mensaje="quiero ver cuanto debo",
                channel="whatsapp",
                recipient="593999",
                session_id="cc-contextual-route-1",
            ),
            state=state,
        )
    )

    assert result["conversation_mode"] == "task"
    assert result["business_domain"] == "billing"
    assert result["reason"] == "contextual_billing"
    assert result["source"] == "turn_interpreter"


def test_conversation_classifier_keeps_active_domain_for_short_contextual_reply():
    classifier = ConversationClassifier()
    state = SessionState(session_id="cc-active-domain-billing-1", current_intent="billing", last_agent="billing")
    state.history.extend(
        [
            {"role": "assistant", "content": 'Si prefiere, escriba "Registrar Pago" o "Link de Cobro".'},
            {"role": "user", "content": "ok"},
        ]
    )

    result = asyncio.run(
        classifier.classify(
            message=InboundMessage(
                mensaje="la primera",
                channel="whatsapp",
                recipient="593999",
                session_id="cc-active-domain-billing-1",
            ),
            state=state,
        )
    )

    assert result["conversation_mode"] == "task"
    assert result["business_domain"] == "billing"
    assert result["reason"] == "active_context_billing"
    assert result["source"] == "heuristic"


def test_conversation_classifier_detects_typoed_greeting():
    classifier = ConversationClassifier()

    result = asyncio.run(
        classifier.classify(
            message=InboundMessage(
                mensaje="ola",
                channel="whatsapp",
                recipient="593999",
                session_id="cc-greeting-typo-1",
            ),
            state=SessionState(session_id="cc-greeting-typo-1"),
        )
    )

    assert result["conversation_mode"] == "greeting_only"
    assert result["business_domain"] == "none"


def test_conversation_classifier_can_route_image_receipt_as_payment_proof(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversation_classifier_enabled", True)
    monkeypatch.setattr(settings, "conversation_classifier_mode", "openai")

    class FakeLLM:
        async def classify_attachment_intent(self, **kwargs):
            return {
                "status": "ok",
                "result": {
                    "attachment_intent": "payment_proof",
                    "confidence": 0.91,
                    "reason": "bank_receipt",
                },
            }

        async def classify_conversation(self, **kwargs):
            raise AssertionError("text classifier should not run when attachment proof was already detected")

    classifier = ConversationClassifier(llm=FakeLLM())
    result = asyncio.run(
        classifier.classify(
            message=InboundMessage(
                mensaje="Imagen enviada",
                channel="whatsapp",
                recipient="593999",
                session_id="cc-4",
                attachments=[
                    {
                        "type": "image",
                        "mime_type": "image/jpeg",
                        "filename": "proof.jpg",
                        "base64_data": "ZmFrZQ==",
                    }
                ],
            ),
            state=SessionState(session_id="cc-4"),
        )
    )

    assert result["conversation_mode"] == "task"
    assert result["business_domain"] == "billing"
    assert result["reason"] == "attachment_payment_proof"
    assert result["source"] == "openai"
