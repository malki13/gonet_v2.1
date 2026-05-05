import asyncio

from packages.orchestrator.response_composer import ResponseComposer
from packages.shared.config import get_settings
from packages.shared.schemas import AgentResult, InboundMessage, RouteDecision, SessionState


def test_response_composer_preserves_billing_message_from_agent(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="facturacion",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-billing-1",
    )
    state = SessionState(session_id="composer-billing-1", current_intent="billing", last_agent="billing")
    decision = RouteDecision(agent="billing", intent="billing", confidence=0.9, reason="test")
    result = AgentResult(
        message=(
            "Nancy Mercedes, ya revisé su contrato *800003* y por ahora aparece en estado *cortado*.\n"
            "Ahora mismo registra un valor pendiente de *$116.16 más impuestos*.\n"
            "Si ya realizó el pago, envíeme el comprobante y lo reviso.\n"
            "Si prefiere pagar ahora, Aquí tiene el enlace directo: https://pagos.gonet.ec/payment"
        ),
        intent="billing",
        agent="billing",
        metadata={
            "contract": {
                "code": "800003",
                "partner_name": "Nancy Mercedes",
                "residual": "116.16",
            }
        },
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message == result.message
    assert rewritten.metadata.get("response_composer") is None


def test_response_composer_skips_openai_for_billing_link_copy(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "openai")

    class FakeLLM:
        async def rewrite_text(self, **kwargs):
            return {"status": "ok", "text": "Claro, aquí le ayudo con eso."}

    composer = ResponseComposer(llm=FakeLLM())
    message = InboundMessage(
        mensaje="link de pago",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-openai-1",
    )
    state = SessionState(session_id="composer-openai-1", current_intent="billing", last_agent="billing")
    decision = RouteDecision(agent="billing", intent="billing_link", confidence=0.9, reason="test")
    result = AgentResult(
        message="Claro, aquí tiene el enlace de pago: https://pagos.gonet.ec/payment",
        intent="billing_link",
        agent="billing",
        metadata={"contract": {"code": "800003"}},
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message == result.message
    assert rewritten.metadata.get("response_composer") is None


def test_response_composer_rewrites_duplicate_billing_async_message(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="Adjunto comprobante",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-duplicate-1",
    )
    state = SessionState(session_id="composer-duplicate-1", current_intent="billing", last_agent="billing")
    decision = RouteDecision(agent="billing", intent="billing_async_result", confidence=0.9, reason="test")
    result = AgentResult(
        message="Pude validar el comprobante, pero ese pago ya fue registrado anteriormente. No voy a ingresarlo de nuevo.",
        intent="billing_async_result",
        agent="billing",
        metadata={"contract": {"code": "800003", "partner_name": "Nancy Mercedes"}},
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    lowered = rewritten.message.lower()
    assert "ya está registrado" in lowered or "ya esta registrado" in lowered
    assert "no es válido" in lowered or "no es valido" in lowered


def test_response_composer_does_not_use_openai_for_billing_agent_copy(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "auto")

    captured = {}

    class FakeLLM:
        def enabled(self):
            return True

        async def rewrite_text(self, **kwargs):
            captured.update(kwargs)
            return {
                "status": "ok",
                "text": "Nancy Mercedes, ya revisé su contrato *800003*. Si prefiere, le dejo el enlace de pago aquí mismo: https://pagos.gonet.ec/payment",
            }

    composer = ResponseComposer(llm=FakeLLM())
    message = InboundMessage(
        mensaje="link de pago",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-auto-openai-1",
    )
    state = SessionState(session_id="composer-auto-openai-1", current_intent="billing", last_agent="billing")
    decision = RouteDecision(agent="billing", intent="billing_link", confidence=0.9, reason="test")
    result = AgentResult(
        message="Claro, aquí tiene el enlace de pago: https://pagos.gonet.ec/payment",
        intent="billing_link",
        agent="billing",
        metadata={"contract": {"code": "800003", "partner_name": "Nancy Mercedes"}},
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message == result.message
    assert rewritten.metadata.get("response_composer") is None
    assert captured == {}


def test_response_composer_can_be_disabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", False)

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="hola",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-disabled-1",
    )
    state = SessionState(session_id="composer-disabled-1")
    decision = RouteDecision(agent="sales", intent="sales", confidence=0.9, reason="test")
    result = AgentResult(
        message="Respuesta demo",
        intent="sales",
        agent="sales",
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message == "Respuesta demo"


def test_response_composer_applies_response_plan_message_even_if_renderer_is_disabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", False)

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="el internet esta lento",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-response-plan-1",
    )
    state = SessionState(session_id="composer-response-plan-1")
    decision = RouteDecision(agent="support", intent="support_network_monitoring", confidence=0.9, reason="test")
    result = AgentResult(
        message="Texto base seco",
        intent="support_network_monitoring",
        agent="support",
        metadata={
            "response_plan": {
                "domain": "support",
                "conversation_state": "monitoring_result",
                "message": "Ya revisé su contrato y veo bastante carga en el wifi. Hagamos una prueba con un solo equipo cerca del router.",
                "reply_goal": "guiar al cliente con troubleshooting útil",
                "hypothesis": "wifi_load",
                "evidence": ["Equipos conectados: 12"],
                "next_step": "reduce_load_and_isolate_device",
            }
        },
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert "bastante carga en el wifi" in rewritten.message
    assert rewritten.message != "Texto base seco"


def test_response_composer_preserves_contract_selection_copy(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="necesito ayuda",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-contract-selection-1",
    )
    state = SessionState(
        session_id="composer-contract-selection-1",
        current_intent="support",
        last_agent="clarify",
    )
    decision = RouteDecision(agent="support", intent="support", confidence=0.9, reason="test")
    original = (
        "Nancy Mercedes, veo varios contratos asociados a tu cédula. Para no equivocarme, dime cuál revisamos:\n"
        "Respóndeme con el número, por ejemplo *1* o *2*.\n"
        "1. *701177* - Estado: *cortado* - Valor pendiente: *$164.94 más impuestos* - Plan: GOPLUS 400 MBPS\n"
        "2. *800003* - Estado: *cortado* - Valor pendiente: *$116.16 más impuestos* - Plan: GOESSENCIAL 250 MBPS"
    )
    result = AgentResult(
        message=original,
        intent="contract_selection",
        agent="support",
        metadata={"contracts_count": 2},
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message == original
    assert rewritten.metadata.get("response_composer") is None


def test_response_composer_preserves_identity_request_with_tip(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="no se",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-identity-1",
    )
    state = SessionState(session_id="composer-identity-1")
    decision = RouteDecision(agent="clarify", intent="ask_cedula", confidence=0.9, reason="test")
    result = AgentResult(
        message=(
            "Tranqui. Para revisar tu servicio sí necesito la cédula o RUC del titular del contrato. "
            "Mientras la consigues, revisa que la ONU y el router estén encendidos y reinícialos 10 segundos. "
            "Cuando la tengas, me la envías y seguimos."
        ),
        intent="ask_cedula",
        agent="clarify",
        metadata={"pending_agent": "support"},
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message.startswith(("Buenos días.", "Buenas tardes.", "Buenas noches."))
    assert "cédula o RUC del titular del contrato" in rewritten.message
    assert "ONU y el router" in rewritten.message


def test_response_composer_preserves_handoff_identity_request(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="asesor",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-handoff-identity-1",
    )
    state = SessionState(session_id="composer-handoff-identity-1")
    decision = RouteDecision(agent="clarify", intent="ask_cedula", confidence=0.9, reason="test")
    result = AgentResult(
        message="Con gusto lo derivo con un asesor especializado. Para continuar, compártame la cédula o RUC del titular y dejo su caso listo.",
        intent="ask_cedula",
        agent="clarify",
        metadata={"pending_agent": "handoff"},
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    lowered = rewritten.message.lower()
    assert "asesor especializado" in lowered
    assert "cédula o ruc" in lowered or "cedula o ruc" in lowered
    assert "dejo su caso listo" in lowered


def test_response_composer_adds_intro_to_first_operational_identity_reply(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="mi inter no sirve",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-first-operational-intro-1",
    )
    state = SessionState(
        session_id="composer-first-operational-intro-1",
        metadata={"assistant_profile": {"display_name": "Valeria"}},
    )
    state.history.append({"role": "user", "content": "hola"})
    state.history.append({"role": "user", "content": "mi inter no sirve"})
    decision = RouteDecision(agent="clarify", intent="ask_cedula", confidence=0.9, reason="identity_required")
    result = AgentResult(
        message="Compártame la cédula o RUC del titular del contrato y lo reviso.",
        intent="ask_cedula",
        agent="clarify",
        metadata={"pending_agent": "support"},
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    lowered = rewritten.message.lower()
    assert "valeria" in lowered
    assert "gonet" in lowered
    assert "cédula o ruc" in lowered
    assert any(
        marker in lowered
        for marker in ("le atiende", "soy ", "está hablando con", "esta hablando con", "buenos días", "buenas tardes", "buenas noches")
    )


def test_response_composer_greets_on_first_payment_proof_identity_request(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="Imagen enviada",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-proof-identity-1",
    )
    state = SessionState(
        session_id="composer-proof-identity-1",
        metadata={"assistant_profile": {"display_name": "Daniela"}},
    )
    decision = RouteDecision(agent="clarify", intent="ask_cedula", confidence=0.9, reason="test")
    result = AgentResult(
        message=(
            "Buenos días, le atiende Daniela de GoNet. "
            "Ya vi la imagen y parece un comprobante de pago. "
            "Para revisarlo por aquí, compártame la cédula o RUC del titular del contrato."
        ),
        intent="ask_cedula",
        agent="clarify",
        metadata={"pending_agent": "billing"},
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    lowered = rewritten.message.lower()
    assert "daniela" in lowered
    assert "gonet" in lowered
    assert "parece un comprobante de pago" in lowered
    assert any(
        marker in lowered
        for marker in ("le atiende", "soy ", "está hablando con", "esta hablando con", "buenos días", "buenas tardes", "buenas noches")
    )


def test_response_composer_keeps_payment_proof_greeting_even_if_intro_flag_was_pre_marked(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="Imagen enviada",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-proof-identity-2",
    )
    state = SessionState(
        session_id="composer-proof-identity-2",
        metadata={
            "assistant_profile": {"display_name": "Daniela"},
            "assistant_intro_sent": True,
        },
    )
    decision = RouteDecision(agent="clarify", intent="ask_cedula", confidence=0.9, reason="test")
    result = AgentResult(
        message=(
            "Buenos días, soy Daniela de GoNet. "
            "Ya vi la imagen y parece un comprobante de pago. "
            "Para revisarlo por aquí, compártame la cédula o RUC del titular del contrato."
        ),
        intent="ask_cedula",
        agent="clarify",
        metadata={"pending_agent": "billing"},
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    lowered = rewritten.message.lower()
    assert "daniela" in lowered
    assert "gonet" in lowered
    assert "parece un comprobante de pago" in lowered


def test_response_composer_keeps_billing_retry_field_visibility_hint(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="Foto del comprobante",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-billing-retry-1",
    )
    state = SessionState(
        session_id="composer-billing-retry-1",
        metadata={"assistant_profile": {"display_name": "Daniela"}},
    )
    decision = RouteDecision(agent="clarify", intent="billing_async_result", confidence=0.9, reason="test")
    result = AgentResult(
        message=(
            "No pude validar bien el comprobante. "
            "Envíame una foto más clara o el archivo completo nuevamente, por favor."
        ),
        intent="billing_async_result",
        agent="billing",
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    lowered = rewritten.message.lower()
    assert "numero del documento" in lowered or "número del documento" in lowered
    assert "fecha" in lowered
    assert "monto" in lowered
    assert "foto clara" in lowered


def test_response_composer_preserves_information_consent_copy(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="800003",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-consent-1",
    )
    state = SessionState(
        session_id="composer-consent-1",
        current_intent="contract_selection",
        last_agent="clarify",
    )
    decision = RouteDecision(agent="clarify", intent="consent_required", confidence=0.9, reason="test")
    result = AgentResult(
        message="Nancy Mercedes, ya encontré su contrato. ¿Me confirma si acepta el uso de la información de GoNet para continuar por aquí?",
        intent="consent_required",
        agent="clarify",
        metadata={"contract": {"partner_name": "Nancy Mercedes", "code": "800003"}},
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message == result.message
    assert rewritten.metadata.get("response_composer") is None


def test_response_composer_preserves_support_monitoring_copy(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="el internet no sirve",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-support-monitoring-1",
    )
    state = SessionState(
        session_id="composer-support-monitoring-1",
        current_intent="support",
        last_agent="support",
    )
    decision = RouteDecision(agent="support", intent="support", confidence=0.9, reason="test")
    result = AgentResult(
        message=(
            "Ya hice una revisión inicial de tu servicio en el contrato *800003*. "
            "Revisa que la ONU y el router estén encendidos y que los cables estén bien conectados. "
            "Si después de eso el problema sigue, respóndeme *sigue igual* y lo dejo con un asesor especializado."
        ),
        intent="support_network_monitoring",
        agent="support",
        metadata={"contract": {"code": "800003"}, "issue_type": "no_service"},
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message == result.message
    assert rewritten.metadata.get("response_composer") is None


def test_response_composer_support_monitoring_keeps_connected_devices_context(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="esta lento y malo no sirve",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-support-monitoring-devices-1",
    )
    state = SessionState(
        session_id="composer-support-monitoring-devices-1",
        current_intent="support",
        last_agent="support",
    )
    decision = RouteDecision(agent="support", intent="support", confidence=0.9, reason="test")
    result = AgentResult(
        message=(
            "Por el monitoreo de lentitud no detecté una falla base en el contrato *500007*. "
            "Actualmente identifico *12* dispositivos conectados a su red. "
            "Indíqueme si el inconveniente ocurre en todos los dispositivos o solo en uno. "
            "Si después de eso el problema sigue, respóndame *sigue igual* y lo dejo con un asesor especializado."
        ),
        intent="support_network_monitoring",
        agent="support",
        metadata={
            "contract": {"code": "500007", "partner_name": "Christian Montero"},
            "issue_type": "slow_internet",
            "connected_devices": 12,
        },
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    lowered = rewritten.message.lower()
    assert "*12*" in rewritten.message
    assert "equipos" in lowered or "dispositivos" in lowered
    assert "christian" not in lowered
    assert "hola" not in lowered


def test_response_composer_preserves_support_clarify_copy(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="hola",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-support-clarify-1",
    )
    state = SessionState(
        session_id="composer-support-clarify-1",
        current_intent="support",
        last_agent="support",
    )
    decision = RouteDecision(agent="support", intent="support", confidence=0.9, reason="test")
    result = AgentResult(
        message=(
            "Christian Montero, ya vi su contrato *500007*. "
            "Indíqueme qué sucede: si está sin internet, si se va por ratos, si está lento, "
            "si desea cambiar la clave del wifi o si prefiere un asesor especializado."
        ),
        intent="support_clarify",
        agent="support",
        metadata={"contract": {"code": "500007", "partner_name": "Christian Montero"}},
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message == result.message
    assert rewritten.metadata.get("response_composer") is None


def test_response_composer_preserves_sales_choice_copy(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="hola q internet ofrecen?",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-sales-choice-1",
    )
    state = SessionState(session_id="composer-sales-choice-1")
    decision = RouteDecision(agent="sales", intent="sales", confidence=0.9, reason="test")
    result = AgentResult(
        message=(
            "Buenos días, le atiende Daniela de GoNet. "
            "Puedo ayudarte con planes hogar, planes pymes, cobertura y agencias.\n\n"
            "Si lo prefiere, le comparto *todos los planes* o le hago unas preguntas rápidas para darle una *recomendación personalizada*."
        ),
        intent="commercial",
        agent="sales",
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message == result.message
    assert rewritten.metadata.get("response_composer") is None


def test_response_composer_preserves_sales_capture_copy(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="quiero contratar",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-sales-capture-1",
    )
    state = SessionState(
        session_id="composer-sales-capture-1",
        current_intent="commercial",
        last_agent="sales",
    )
    decision = RouteDecision(agent="sales", intent="sales", confidence=0.9, reason="test")
    result = AgentResult(
        message=(
            "Claro, le ayudo. Para avanzar sí necesito unos datos básicos.\n\n"
            "Para empezar, ¿me comparte su nombre completo?"
        ),
        intent="commercial",
        agent="sales",
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message == result.message
    assert rewritten.metadata.get("response_composer") is None


def test_response_composer_skips_openai_for_structured_sales_catalog(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "openai")

    class FakeLLM:
        async def rewrite_text(self, **kwargs):
            raise AssertionError("rewrite_text should not be called for structured sales catalogs")

    composer = ResponseComposer(llm=FakeLLM())
    message = InboundMessage(
        mensaje="todos",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-sales-catalog-1",
    )
    state = SessionState(
        session_id="composer-sales-catalog-1",
        current_intent="commercial",
        last_agent="sales",
    )
    decision = RouteDecision(agent="sales", intent="sales", confidence=0.9, reason="test")
    original = (
        "Te paso las opciones que tengo ahora:\n\n"
        "**Planes hogar disponibles:**\n\n"
        "**GoPrime**\n"
        "- **Velocidad:** **900 Mbps**\n"
        "- **Precio + IMP:** **$29.90**"
    )
    result = AgentResult(
        message=original,
        intent="commercial",
        agent="sales",
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message == original


def test_response_composer_skips_openai_for_sales_recommendation_question(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "auto")

    class FakeLLM:
        def enabled(self):
            return True

        async def rewrite_text(self, **kwargs):
            raise AssertionError("rewrite_text should not be called for guided sales questions")

    composer = ResponseComposer(llm=FakeLLM())
    message = InboundMessage(
        mensaje="q internet ofreces",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-sales-guided-question-1",
    )
    state = SessionState(
        session_id="composer-sales-guided-question-1",
        metadata={"assistant_profile": {"display_name": "Luis"}},
    )
    decision = RouteDecision(agent="sales", intent="sales", confidence=0.9, reason="test")
    result = AgentResult(
        message="Buenos días, soy Luis de GoNet. ¿El internet sería para su casa o para su negocio?",
        intent="commercial",
        agent="sales",
        metadata={
            "response_plan": {
                "domain": "sales",
                "conversation_state": "recommendation_question",
                "message": "Buenos días, soy Luis de GoNet. ¿El internet sería para su casa o para su negocio?",
                "reply_goal": "hacer una asesoría comercial guiada con un asesor especializado sin perder continuidad",
            }
        },
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message == result.message


def test_response_composer_keeps_guided_sales_question_clean_after_prior_intro(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "auto")

    class FakeLLM:
        def enabled(self):
            return True

        async def rewrite_text(self, **kwargs):
            raise AssertionError("rewrite_text should not be called for guided sales questions after greeting")

    composer = ResponseComposer(llm=FakeLLM())
    message = InboundMessage(
        mensaje="q internet ofreces",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-sales-guided-question-after-intro-1",
    )
    state = SessionState(
        session_id="composer-sales-guided-question-after-intro-1",
        metadata={
            "assistant_profile": {"display_name": "Luis"},
            "assistant_intro_sent": True,
        },
    )
    state.history.append({"role": "assistant", "content": "Buenos días, le atiende Luis de GoNet. ¿En qué le ayudo?"})
    decision = RouteDecision(agent="sales", intent="sales", confidence=0.9, reason="test")
    result = AgentResult(
        message="¿El internet sería para su casa o para su negocio?",
        intent="commercial",
        agent="sales",
        metadata={
            "response_plan": {
                "domain": "sales",
                "conversation_state": "recommendation_question",
                "message": "¿El internet sería para su casa o para su negocio?",
                "reply_goal": "hacer una asesoría comercial guiada con un asesor especializado sin perder continuidad",
            }
        },
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message == "¿El internet sería para su casa o para su negocio?"


def test_response_composer_adds_intro_to_first_sales_reply(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="q planes ofrecen",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-first-sales-intro-1",
    )
    state = SessionState(
        session_id="composer-first-sales-intro-1",
        metadata={"assistant_profile": {"display_name": "Valeria"}},
    )
    state.history.append({"role": "user", "content": "q planes ofrecen"})
    decision = RouteDecision(agent="sales", intent="sales", confidence=0.9, reason="test")
    result = AgentResult(
        message="Puedo ayudarte con planes hogar, planes pymes, cobertura y agencias.",
        intent="commercial",
        agent="sales",
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    lowered = rewritten.message.lower()
    assert "valeria" in lowered
    assert "gonet" in lowered
    assert "planes hogar" in lowered


def test_response_composer_humanizes_session_closed_notice(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "conversational_renderer_enabled", True)
    monkeypatch.setattr(settings, "conversational_renderer_mode", "heuristic")

    composer = ResponseComposer()
    message = InboundMessage(
        mensaje="",
        channel="whatsapp",
        recipient="593111",
        session_id="composer-session-closed-1",
    )
    state = SessionState(
        session_id="composer-session-closed-1",
        channel="whatsapp",
        recipient="593111",
        current_intent="support",
        last_agent="support",
    )
    state.history.append({"role": "user", "content": "hola"})
    state.history.append({"role": "assistant", "content": "Sí, le ayudo con eso."})
    decision = RouteDecision(agent="clarify", intent="session_closed", confidence=1.0, reason="inactivity_close")
    result = AgentResult(
        message="Voy a cerrar este chat por inactividad. Si luego necesitas algo más, escríbenos por aquí y retomamos.",
        intent="session_closed",
        agent="clarify",
        metadata={"human_handoff": False},
    )

    rewritten = asyncio.run(composer.compose(message=message, state=state, decision=decision, result=result))

    assert rewritten.message != result.message
    assert "inactividad" in rewritten.message.lower()
    assert "retom" in rewritten.message.lower()
