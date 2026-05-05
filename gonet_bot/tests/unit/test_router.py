import asyncio

from packages.orchestrator.router import IntentRouter
from packages.shared.schemas import InboundMessage, SessionState


def test_router_detects_billing_keywords():
    router = IntentRouter()
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="Quiero registrar un pago",
                channel="whatsapp",
                recipient="593999",
                session_id="s1",
            ),
            SessionState(session_id="s1"),
        )
    )
    assert decision.agent == "billing"
    assert decision.intent == "billing"


def test_router_requests_clarification_when_signal_is_low():
    router = IntentRouter()
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="hola",
                channel="whatsapp",
                recipient="593999",
                session_id="s2",
            ),
            SessionState(session_id="s2"),
        )
    )
    assert decision.agent == "clarify"
    assert decision.requires_clarification is True
    assert decision.reason == "greeting_only"


def test_router_detects_support_keywords_for_slow_internet():
    router = IntentRouter()
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="El internet está muy lento desde ayer",
                channel="whatsapp",
                recipient="593999",
                session_id="s3",
            ),
            SessionState(session_id="s3"),
        )
    )
    assert decision.agent == "support"
    assert decision.intent == "support"


def test_router_routes_publication_interest_to_sales():
    router = IntentRouter()
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="vi una publicacion de internet",
                channel="whatsapp",
                recipient="593999",
                session_id="s3b",
            ),
            SessionState(session_id="s3b"),
        )
    )
    assert decision.agent == "sales"
    assert decision.intent == "sales"


def test_router_marks_out_of_scope_topics_for_clarification():
    router = IntentRouter()
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="Cuéntame un chiste de fútbol",
                channel="whatsapp",
                recipient="593999",
                session_id="s4",
            ),
            SessionState(session_id="s4"),
        )
    )
    assert decision.agent == "clarify"
    assert decision.reason == "out_of_scope"


def test_router_detects_small_talk_for_brief_human_reply():
    router = IntentRouter()
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="Que tal tu día?",
                channel="whatsapp",
                recipient="593999",
                session_id="s4b",
            ),
            SessionState(session_id="s4b"),
        )
    )
    assert decision.agent == "clarify"
    assert decision.reason == "small_talk"


def test_router_rejects_messages_that_are_too_long():
    router = IntentRouter()
    very_long = "hola " * 200
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje=very_long,
                channel="whatsapp",
                recipient="593999",
                session_id="s5",
            ),
            SessionState(session_id="s5"),
        )
    )
    assert decision.agent == "clarify"
    assert decision.reason == "message_too_long"


def test_router_keeps_commercial_context_for_followup_questions():
    router = IntentRouter()
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="Y para hogar?",
                channel="whatsapp",
                recipient="593999",
                session_id="s6",
            ),
            SessionState(session_id="s6", current_intent="commercial"),
        )
    )
    assert decision.agent == "sales"
    assert decision.intent == "sales"
    assert decision.reason in {"commercial_followup_context", "sales_intent_plan"}


def test_router_does_not_force_handoff_for_incidental_asesor_reference():
    router = IntentRouter()
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="Quiero información del asesor comercial sobre planes hogar",
                channel="whatsapp",
                recipient="593999",
                session_id="s7",
            ),
            SessionState(session_id="s7"),
        )
    )
    assert decision.agent == "sales"
    assert decision.intent == "sales"


def test_router_forces_handoff_for_bare_asesor_request():
    router = IntentRouter()
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="asesor",
                channel="whatsapp",
                recipient="593999",
                session_id="s7b",
            ),
            SessionState(session_id="s7b"),
        )
    )
    assert decision.agent == "handoff"
    assert decision.intent == "human_handoff"


def test_router_detects_whatsapp_abbreviated_commercial_discovery():
    router = IntentRouter()
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="q ofrecen?",
                channel="whatsapp",
                recipient="593999",
                session_id="s8",
            ),
            SessionState(session_id="s8"),
        )
    )
    assert decision.agent == "sales"
    assert decision.intent == "sales"
    assert decision.reason == "commercial_discovery_keywords"


def test_router_detects_typoed_catalog_question_as_sales():
    router = IntentRouter()
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="q proms tinees?",
                channel="whatsapp",
                recipient="593999",
                session_id="s8b",
            ),
            SessionState(session_id="s8b"),
        )
    )
    assert decision.agent == "sales"
    assert decision.intent == "sales"
    assert decision.reason == "sales_intent_plan"


def test_router_routes_que_internets_ofrecen_to_sales():
    router = IntentRouter()
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="hola q internets ofrecen?",
                channel="whatsapp",
                recipient="593999",
                session_id="s8bb",
            ),
            SessionState(session_id="s8bb"),
        )
    )
    assert decision.agent == "sales"
    assert decision.intent == "sales"
    assert decision.reason == "commercial_discovery_keywords"


def test_router_treats_ola_as_greeting_only():
    router = IntentRouter()
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="ola",
                channel="whatsapp",
                recipient="593999",
                session_id="s8c",
            ),
            SessionState(session_id="s8c"),
        )
    )
    assert decision.agent == "clarify"
    assert decision.reason == "greeting_only"


def test_router_maps_semantic_classifier_task_to_business_agent():
    class StubClassifier:
        async def classify(self, *, message, state):
            return {
                "conversation_mode": "task",
                "business_domain": "sales",
                "confidence": 0.91,
                "reason": "semantic_sales",
            }

    router = IntentRouter(classifier=StubClassifier())
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="Estoy buscando el mejor plan para mi casa",
                channel="whatsapp",
                recipient="593999",
                session_id="s9",
            ),
            SessionState(session_id="s9"),
        )
    )

    assert decision.agent == "sales"
    assert decision.intent == "sales"
    assert decision.reason == "semantic_sales"


def test_router_maps_semantic_classifier_small_talk_to_clarify():
    class StubClassifier:
        async def classify(self, *, message, state):
            return {
                "conversation_mode": "small_talk",
                "business_domain": "none",
                "confidence": 0.73,
                "reason": "small_talk",
            }

    router = IntentRouter(classifier=StubClassifier())
    decision = asyncio.run(
        router.decide(
            InboundMessage(
                mensaje="¿Qué tal tu día?",
                channel="whatsapp",
                recipient="593999",
                session_id="s10",
            ),
            SessionState(session_id="s10"),
        )
    )

    assert decision.agent == "clarify"
    assert decision.intent == "clarify"
    assert decision.requires_clarification is True
    assert decision.reason == "small_talk"
