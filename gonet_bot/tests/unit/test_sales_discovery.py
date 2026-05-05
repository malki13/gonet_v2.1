import asyncio

from packages.agents.sales.service import SalesAgent
from packages.integrations.agencies_repo import AgenciesRepo
from packages.integrations.promotions_api import PromotionsAPI
from packages.shared.schemas import InboundMessage, SessionState


GUIDED_CATALOG = {
    "data": {
        "GONECTADOS": [
            {"name": "GoEssencial", "mbps": "600", "price": "19.90", "details": [{"name": "600 megas por siempre"}]},
            {"name": "GoPlus", "mbps": "750", "price": "23.90", "details": [{"name": "750 megas por siempre"}]},
            {"name": "GoPrime", "mbps": "900", "price": "29.90", "details": [{"name": "Velocidad simétrica"}]},
            {"name": "GoConnect", "mbps": "1000", "price": "34.90", "details": [{"name": "Incluye GoMax Ultra"}]},
        ],
        "PYMES": [
            {"name": "Pyme 300", "mbps": "300", "price": "39.99", "details": [{"name": "Soporte empresarial"}]},
        ],
    }
}


def test_sales_agent_requests_city_for_agency_without_guessing_location(monkeypatch):
    async def should_not_query_city(self, city_upper: str):
        raise AssertionError("no debería consultar agencias por ciudad sin una ubicación válida")

    async def should_not_query_province(self, province_upper: str):
        raise AssertionError("no debería consultar agencias por provincia sin una ubicación válida")

    monkeypatch.setattr(AgenciesRepo, "by_city", should_not_query_city)
    monkeypatch.setattr(AgenciesRepo, "by_province", should_not_query_province)

    agent = SalesAgent()
    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="quiero ir a una agencia",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-agency-no-location-1",
            ),
            SessionState(session_id="sales-agency-no-location-1"),
        )
    )

    lowered = result.message.lower()
    assert result.intent == "agencies"
    assert "ciudad o provincia" in lowered
    assert "no encuentro agencias" not in lowered


def test_sales_agent_omits_phone_number_when_showing_agencies(monkeypatch):
    async def fake_by_province(self, province_upper: str):
        if province_upper == "AZUAY":
            return [
                {
                    "agencia": "Agencia Cuenca Centro",
                    "ciudad": "Cuenca",
                    "direccion": "Gran Colombia y Borrero",
                    "horarios": "Lun a Vie 08:00 - 17:00",
                    "telefono": "",
                }
            ]
        return []

    monkeypatch.setattr(AgenciesRepo, "by_province", fake_by_province)

    agent = SalesAgent()
    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="Quiero saber las agencias del Azuay",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-agency-phone-omit-1",
            ),
            SessionState(session_id="sales-agency-phone-omit-1"),
        )
    )

    lowered = result.message.lower()
    assert result.intent == "agencies"
    assert "agencia cuenca centro" in lowered
    assert "gran colombia y borrero" in lowered
    assert "teléfono" not in lowered
    assert "telefono" not in lowered


def test_sales_agent_shows_catalog_when_customer_asks_for_promotions_mid_recommendation(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-promotions-mid-recommendation-1")

    for text in ("q internet tienes", "para mi casa"):
        result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-promotions-mid-recommendation-1",
                ),
                state,
            )
        )
        state.current_intent = result.intent

    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="y que promociones tiene",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-promotions-mid-recommendation-1",
            ),
            state,
        )
    )

    lowered = result.message.lower()
    assert result.intent == "commercial"
    assert "promociones y planes disponibles" in lowered
    assert ", le comparto" not in lowered
    assert "planes hogar" in lowered
    assert "goessencial" in lowered
    assert "cuántas personas" not in lowered and "cuantas personas" not in lowered


def test_sales_agent_shows_catalog_when_customer_says_only_wants_plans_mid_recommendation(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-only-plans-mid-recommendation-1")

    for text in ("q internet tienes", "para mi casa"):
        result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-only-plans-mid-recommendation-1",
                ),
                state,
            )
        )
        state.current_intent = result.intent

    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="no joda, solo quiero los planes",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-only-plans-mid-recommendation-1",
            ),
            state,
        )
    )

    lowered = result.message.lower()
    assert result.intent == "commercial"
    assert "estos son los planes" in lowered or "planes hogar disponibles" in lowered
    assert ", le comparto" not in lowered
    assert "cuántas personas" not in lowered and "cuantas personas" not in lowered


def test_sales_agent_shows_catalog_when_customer_demands_plans_mid_recommendation(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-demand-plans-mid-recommendation-1")

    for text in ("q internet tienes", "para mi casa"):
        result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-demand-plans-mid-recommendation-1",
                ),
                state,
            )
        )
        state.current_intent = result.intent

    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="no jodas y dame los planes",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-demand-plans-mid-recommendation-1",
            ),
            state,
        )
    )

    lowered = result.message.lower()
    assert result.intent == "commercial"
    assert "goessencial" in lowered or "goplus" in lowered or "goprime" in lowered
    assert "cuántas personas" not in lowered and "cuantas personas" not in lowered
    assert "¿el internet sería para su casa o para su negocio?" not in lowered


def test_sales_agent_keeps_catalog_when_customer_asks_promotions_after_full_catalog(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-promotions-after-catalog-1")

    first = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="no joda solo quiero los planes",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-promotions-after-catalog-1",
            ),
            state,
        )
    )
    assert "planes hogar" in first.message.lower()

    second = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="y que promociones tiene",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-promotions-after-catalog-1",
            ),
            state,
        )
    )

    lowered = second.message.lower()
    assert second.intent == "commercial"
    assert "promociones y planes disponibles" in lowered
    assert "cuántas personas" not in lowered and "cuantas personas" not in lowered
    assert "dispositivos" not in lowered or "cuántos dispositivos" not in lowered


def test_sales_agent_answers_whatsapp_abbreviated_discovery():
    agent = SalesAgent()
    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="q ofrecen?",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-discovery-short-1",
            ),
            SessionState(session_id="sales-discovery-short-1"),
        )
    )
    assert result.agent == "sales"
    assert result.intent == "commercial"
    lowered = result.message.lower()
    assert "no te puedo ayudar" not in lowered
    assert "sí, te ayudo" not in lowered and "si, te ayudo" not in lowered
    assert "internet sería para su casa o para su negocio" in lowered
    assert "catálogo de golpe" not in lowered and "catalogo de golpe" not in lowered
    assert ((result.metadata.get("response_plan") or {}).get("message") or "").strip() == result.message.strip()


def test_sales_agent_guided_question_after_prior_greeting_does_not_repeat_empty_opener():
    agent = SalesAgent()
    state = SessionState(session_id="sales-discovery-after-greeting-1")
    state.metadata["assistant_profile"] = {"display_name": "Luis"}
    state.metadata["assistant_intro_sent"] = True

    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="q internet ofreces",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-discovery-after-greeting-1",
            ),
            state,
        )
    )

    assert result.message == "Con gusto le ayudo.\n\n¿El internet sería para su casa o para su negocio?"
    assert ((result.metadata.get("response_plan") or {}).get("message") or "").strip() == result.message.strip()


def test_sales_agent_treats_promotions_question_as_commercial_request():
    agent = SalesAgent()
    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="q promociones tienes?",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-promo-short-1",
            ),
            SessionState(session_id="sales-promo-short-1"),
        )
    )
    assert result.agent == "sales"
    assert result.intent == "commercial"
    assert "no te puedo ayudar" not in result.message.lower()
    assert "internet sería para su casa o para su negocio" in result.message.lower()


def test_sales_agent_treats_promos_shorthand_as_commercial_request():
    agent = SalesAgent()
    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="q promos tienes?",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-promos-short-1",
            ),
            SessionState(session_id="sales-promos-short-1"),
        )
    )
    assert result.agent == "sales"
    assert result.intent == "commercial"
    assert "no te puedo ayudar" not in result.message.lower()
    assert "internet sería para su casa o para su negocio" in result.message.lower()


def test_sales_agent_treats_typoed_promotions_question_as_commercial_request():
    agent = SalesAgent()
    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="q proms tinees?",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-promos-typo-1",
            ),
            SessionState(session_id="sales-promos-typo-1"),
        )
    )
    assert result.agent == "sales"
    assert result.intent == "commercial"
    assert "no te puedo ayudar" not in result.message.lower()
    assert "internet sería para su casa o para su negocio" in result.message.lower()


def test_sales_agent_recommends_specific_plan_after_guided_questions(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-guided-rec-1")

    first = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="q promos tienes?",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-rec-1",
            ),
            state,
        )
    )
    assert "internet sería para su casa o para su negocio" in first.message.lower()

    second = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="hogar",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-rec-1",
            ),
            state,
        )
    )
    assert "cuántas personas" in second.message.lower() or "cuantas personas" in second.message.lower()

    third = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="4 personas",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-rec-1",
            ),
            state,
        )
    )
    assert "cuántos dispositivos" in third.message.lower() or "cuantos dispositivos" in third.message.lower()

    fourth = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="15 dispositivos",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-rec-1",
            ),
            state,
        )
    )
    assert "pequeño, mediano o grande" in fourth.message.lower() or "pequeno, mediano o grande" in fourth.message.lower()

    fifth = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="grande",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-rec-1",
            ),
            state,
        )
    )
    assert "streaming" in fifth.message.lower() or "gaming" in fifth.message.lower()

    sixth = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="gaming",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-rec-1",
            ),
            state,
        )
    )
    lowered = sixth.message.lower()
    assert "goconnect" in lowered
    assert "1000" in lowered
    assert "le recomendaría" in lowered or "le recomendaria" in lowered
    assert "4 personas" in lowered
    assert "15 dispositivos" in lowered
    assert "**velocidad:**" in lowered
    assert "**precio + imp:**" in lowered
    assert "todas las opciones" in lowered
    assert "avancemos con este" in lowered or "te muestro los demás" in lowered or "te muestro los demas" in lowered


def test_sales_agent_keeps_capture_flow_for_explicit_purchase_intent():
    agent = SalesAgent()
    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="Quiero contratar un plan de internet",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-explicit-capture-1",
            ),
            SessionState(session_id="sales-explicit-capture-1"),
        )
    )
    assert result.intent == "commercial"
    assert "nombre completo" in result.message.lower()
    assert "planes o agencias" in result.message.lower()


def test_sales_agent_turns_recommended_plan_acceptance_into_capture(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-guided-accept-1")

    steps = ("q promos tienes?", "hogar", "4 personas", "4 dispositivos", "mediano", "gaming")
    last_result = None
    for text in steps:
        last_result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-guided-accept-1",
                ),
                state,
            )
        )
        state.current_intent = last_result.intent

    assert last_result is not None
    assert "goprime" in last_result.message.lower()

    accepted = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="ese me sirve",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-accept-1",
            ),
            state,
        )
    )

    lowered = accepted.message.lower()
    assert accepted.intent == "commercial"
    assert "goprime" in lowered
    assert "nombre completo" in lowered
    assert "lo dejamos avanzado" in lowered
    assert "no te puedo ayudar" not in lowered
    assert state.metadata["sales"]["awaiting_crm_field"] == "partner_name"


def test_sales_agent_treats_bare_ese_as_acceptance_of_recommended_plan(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-guided-accept-ese-1")

    steps = ("q promos tienes?", "hogar", "4 personas", "4 dispositivos", "mediano", "gaming")
    last_result = None
    for text in steps:
        last_result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-guided-accept-ese-1",
                ),
                state,
            )
        )
        state.current_intent = last_result.intent

    assert last_result is not None
    assert "goprime" in last_result.message.lower()

    accepted = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="ese",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-accept-ese-1",
            ),
            state,
        )
    )

    lowered = accepted.message.lower()
    assert accepted.intent == "commercial"
    assert "goprime" in lowered
    assert "nombre completo" in lowered
    assert "no te puedo ayudar" not in lowered
    assert state.metadata["sales"]["awaiting_crm_field"] == "partner_name"


def test_sales_agent_treats_ese_sirve_as_acceptance_of_recommended_plan(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-guided-accept-ese-sirve-1")

    steps = ("q promos tienes?", "hogar", "2 personas", "3 dispositivos", "mediano", "streaming")
    last_result = None
    for text in steps:
        last_result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-guided-accept-ese-sirve-1",
                ),
                state,
            )
        )
        state.current_intent = last_result.intent

    assert last_result is not None
    assert "goplus" in last_result.message.lower()

    accepted = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="ese sirve",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-accept-ese-sirve-1",
            ),
            state,
        )
    )

    lowered = accepted.message.lower()
    assert accepted.intent == "commercial"
    assert "goplus" in lowered
    assert "nombre completo" in lowered
    assert "no te puedo ayudar" not in lowered
    assert state.metadata["sales"]["awaiting_crm_field"] == "partner_name"


def test_sales_agent_shows_higher_speed_options_after_recommended_plan(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-guided-more-megas-1")

    steps = (
        "q internet ofreces",
        "hogar",
        "2 personas",
        "3 dispositivos",
        "departamento pequeño",
        "streaming",
    )
    last_result = None
    for text in steps:
        last_result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-guided-more-megas-1",
                ),
                state,
            )
        )
        state.current_intent = last_result.intent

    assert last_result is not None
    assert "goessencial" in last_result.message.lower()

    followup = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="tienes otros planes con mas megas?",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-more-megas-1",
            ),
            state,
        )
    )

    lowered = followup.message.lower()
    assert "opciones con más megas" in lowered or "opciones con mas megas" in lowered
    assert "goplus" in lowered
    assert "goprime" in lowered
    assert "goconnect" in lowered
    assert "goessencial" not in lowered
    catalog_context = (state.metadata.get("sales") or {}).get("catalog_context") or {}
    plans = catalog_context.get("plans") or []
    assert plans
    assert all((plan.get("mbps_value") or 0) > 600 for plan in plans)


def test_sales_agent_keeps_recommended_plan_context_when_reply_is_ambiguous(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-guided-unclear-followup-1")

    steps = ("q promos tienes?", "hogar", "2 personas", "3 dispositivos", "mediano", "streaming")
    last_result = None
    for text in steps:
        last_result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-guided-unclear-followup-1",
                ),
                state,
            )
        )
        state.current_intent = last_result.intent

    assert last_result is not None
    assert "goplus" in last_result.message.lower()

    followup = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="como asi?",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-unclear-followup-1",
            ),
            state,
        )
    )

    lowered = (followup.message or "").lower()
    assert followup.intent == "commercial"
    assert "goplus" in lowered
    assert "le muestro los demás" in lowered or "le muestro los demas" in lowered
    assert "no te puedo ayudar" not in lowered


def test_sales_agent_offers_catalog_or_personalized_recommendation_when_data_is_declined():
    agent = SalesAgent()
    state = SessionState(session_id="sales-info-choice-1")

    first = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="hola q internet ofrecen?",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-info-choice-1",
            ),
            state,
        )
    )
    state.current_intent = first.intent
    lowered = first.message.lower()
    assert "internet sería para su casa o para su negocio" in lowered
    assert "catálogo de golpe" not in lowered and "catalogo de golpe" not in lowered

    second = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="no quiero mis datos",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-info-choice-1",
            ),
            state,
        )
    )

    lowered = second.message.lower()
    assert "internet sería para su casa o para su negocio" in lowered
    assert "no necesito sus datos" in lowered
    assert "nombre completo" not in lowered
    assert "datos básicos" not in lowered and "datos basicos" not in lowered
    assert state.metadata["sales"]["awaiting_info_choice"] is False
    assert state.metadata["sales"]["awaiting_recommendation_field"] == "segment"


def test_sales_agent_resolves_catalog_plan_by_price_and_advances_to_capture(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-catalog-selection-1")

    for text in ("quiero planes hogar", "todos"):
        result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-catalog-selection-1",
                ),
                state,
            )
        )
        state.current_intent = result.intent

    selected = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="el de 19.90",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-catalog-selection-1",
            ),
            state,
        )
    )
    state.current_intent = selected.intent

    lowered = (selected.message or "").lower()
    assert selected.intent == "commercial"
    assert "goessencial" in lowered
    assert "19.90" in selected.message
    assert "opción más accesible" in selected.message.lower() or "opcion mas accesible" in lowered

    accepted = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="quiero ese",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-catalog-selection-1",
            ),
            state,
        )
    )

    lowered = (accepted.message or "").lower()
    assert accepted.intent == "commercial"
    assert "goessencial" in lowered
    assert "nombre completo" in lowered
    assert state.metadata["sales"]["awaiting_crm_field"] == "partner_name"


def test_sales_agent_starts_personalized_recommendation_after_info_choice(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-info-choice-rec-1")

    for text in ("hola q internet ofrecen?", "no quiero mis datos"):
        result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-info-choice-rec-1",
                ),
                state,
            )
        )
        state.current_intent = result.intent

    third = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="para mi casa",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-info-choice-rec-1",
            ),
            state,
        )
    )

    lowered = (third.message or "").lower()
    assert "su casa" in lowered
    assert "cuántas personas" in lowered or "cuantas personas" in lowered
    assert "entonces sería" not in lowered
    assert state.metadata["sales"]["awaiting_info_choice"] is False
    assert state.metadata["sales"]["recommendation_profile"]["segment"] == "residential"


def test_sales_agent_guided_questions_do_not_sound_like_form_acknowledgements(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-guided-tone-1")

    first = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="q internet ofreces",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-tone-1",
            ),
            state,
        )
    )
    state.current_intent = first.intent

    second = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="para mi casa",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-tone-1",
            ),
            state,
        )
    )
    state.current_intent = second.intent

    third = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="3",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-tone-1",
            ),
            state,
        )
    )

    assert "entonces sería" not in (second.message or "").lower()
    assert "entonces sería" not in (third.message or "").lower()
    assert "¿y más o menos cuántos dispositivos" in (third.message or "").lower() or "¿y mas o menos cuantos dispositivos" in (third.message or "").lower()


def test_sales_agent_uses_active_turn_interpreter_for_ambiguous_info_choice(monkeypatch):
    class FakeLLM:
        def enabled(self):
            return True

        async def extract_json(self, **kwargs):
            payload = (kwargs or {}).get("payload") or {}
            flow_context = payload.get("flow_context") or {}
            message = (payload.get("message") or {}).get("normalized")
            if flow_context.get("current_stage") == "info_choice" and message == "lo que tu veas mejor":
                return {
                    "status": "ok",
                    "result": {
                        "action": "start_recommendation",
                        "confidence": 0.92,
                        "reason": "choice_in_context",
                    },
                }
            return {"status": "ok", "result": {}}

    agent = SalesAgent(llm=FakeLLM())
    state = SessionState(session_id="sales-info-choice-contextual-1")

    for text in ("hola q internet ofrecen?", "no quiero mis datos"):
        result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-info-choice-contextual-1",
                ),
                state,
            )
        )
        state.current_intent = result.intent

    third = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="lo que tu veas mejor",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-info-choice-contextual-1",
            ),
            state,
        )
    )

    lowered = (third.message or "").lower()
    assert "internet sería para su casa o para su negocio" in lowered
    assert state.metadata["sales"]["awaiting_info_choice"] is False
    assert state.metadata["sales"]["awaiting_recommendation_field"] == "segment"


def test_sales_agent_shows_full_catalog_after_info_choice(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-info-choice-catalog-1")

    for text in ("hola q internet ofrecen?", "no quiero mis datos"):
        result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-info-choice-catalog-1",
                ),
                state,
            )
        )
        state.current_intent = result.intent

    third = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="todos",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-info-choice-catalog-1",
            ),
            state,
        )
    )

    lowered = (third.message or "").lower()
    assert "estos son los planes" in lowered
    assert "planes hogar disponibles" in lowered
    assert state.metadata["sales"]["awaiting_info_choice"] is False
    assert state.metadata["sales"]["commercial_registration_declined"] is True


def test_sales_agent_keeps_device_count_when_space_answer_contains_una(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-guided-devices-1")

    steps = (
        "q promos tienes?",
        "hogar",
        "3 personas",
        "para 7 dispositivos",
        "es una casa grande",
        "ver peliculas, jugar y para deberes",
    )
    last_result = None
    for text in steps:
        last_result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-guided-devices-1",
                ),
                state,
            )
        )
        state.current_intent = last_result.intent

    lowered = (last_result.message or "").lower()
    assert "3 personas" in lowered
    assert "7 dispositivos" in lowered
    assert "1 dispositivos" not in lowered


def test_sales_agent_treats_phone_like_words_as_devices_in_recommendation_flow(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-guided-device-words-1")

    steps = (
        "q planes ofrecen?",
        "quiero un internet para mi casa",
        "para 2",
    )
    last_result = None
    for text in steps:
        last_result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-guided-device-words-1",
                ),
                state,
            )
        )
        state.current_intent = last_result.intent

    assert last_result is not None
    assert "cuantos dispositivos" in last_result.message.lower() or "cuántos dispositivos" in last_result.message.lower()

    devices_reply = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="una tele y dos telefonos",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-device-words-1",
            ),
            state,
        )
    )

    lowered = (devices_reply.message or "").lower()
    assert "contact center" not in lowered
    assert "pequeño, mediano o grande" in lowered or "pequeno, mediano o grande" in lowered
    assert state.metadata["sales"]["recommendation_profile"]["devices"] == 3


def test_sales_agent_uses_active_turn_interpreter_to_keep_context_on_ambiguous_device_reply(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    class FakeLLM:
        def enabled(self):
            return True

        async def extract_json(self, **kwargs):
            payload = (kwargs or {}).get("payload") or {}
            flow_context = payload.get("flow_context") or {}
            recommendation_context = payload.get("recommendation_context") or {}
            message = (payload.get("message") or {}).get("normalized")
            if (
                flow_context.get("current_stage") == "recommendation_question"
                and message == "mi telefono y el de mi esposa ademas del televisor"
            ):
                return {
                    "status": "ok",
                    "result": {
                        "action": "answer_current_step",
                        "confidence": 0.91,
                        "reason": "devices_answer_in_context",
                    },
                }
            if recommendation_context and message == "mi telefono y el de mi esposa ademas del televisor":
                return {"status": "ok", "result": {"devices": 3}}
            return {"status": "ok", "result": {}}

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent(llm=FakeLLM())
    state = SessionState(session_id="sales-guided-contextual-devices-1")

    steps = (
        "q planes ofrecen?",
        "quiero internet para mi casa",
        "para 2",
    )
    last_result = None
    for text in steps:
        last_result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-guided-contextual-devices-1",
                ),
                state,
            )
        )
        state.current_intent = last_result.intent

    assert last_result is not None
    assert "cuantos dispositivos" in last_result.message.lower() or "cuántos dispositivos" in last_result.message.lower()

    devices_reply = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="mi telefono y el de mi esposa ademas del televisor",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-contextual-devices-1",
            ),
            state,
        )
    )

    lowered = (devices_reply.message or "").lower()
    assert "contact center" not in lowered
    assert "pequeño, mediano o grande" in lowered or "pequeno, mediano o grande" in lowered
    assert state.metadata["sales"]["recommendation_profile"]["devices"] == 3


def test_sales_agent_accepts_para_1_as_people_answer(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-guided-para-1-1")

    first = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="q promos tienes?",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-para-1-1",
            ),
            state,
        )
    )
    state.current_intent = first.intent

    second = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="hogar",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-para-1-1",
            ),
            state,
        )
    )
    state.current_intent = second.intent

    third = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="para 1",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-para-1-1",
            ),
            state,
        )
    )

    lowered = (third.message or "").lower()
    assert "cuántos dispositivos" in lowered or "cuantos dispositivos" in lowered
    assert state.metadata["sales"]["recommendation_profile"]["people"] == 1


def test_sales_agent_accepts_range_as_devices_answer(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent()
    state = SessionState(session_id="sales-guided-range-devices-1")

    for text in ("q promos tienes?", "hogar", "1 persona"):
        result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-guided-range-devices-1",
                ),
                state,
            )
        )
        state.current_intent = result.intent

    range_reply = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="3-4 aprox",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-range-devices-1",
            ),
            state,
        )
    )

    lowered = (range_reply.message or "").lower()
    assert "pequeño, mediano o grande" in lowered or "pequeno, mediano o grande" in lowered
    assert state.metadata["sales"]["recommendation_profile"]["devices"] == 4


def test_sales_agent_uses_semantic_extractor_for_conversational_slot_reply(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    class FakeLLM:
        def enabled(self):
            return True

        async def extract_json(self, **kwargs):
            normalized = (((kwargs or {}).get("payload") or {}).get("message") or {}).get("normalized")
            if normalized == "vivo solo pero se conectan como 4 equipos":
                return {
                    "status": "ok",
                    "result": {
                        "people": 1,
                        "devices": 4,
                    },
                }
            return {"status": "ok", "result": {}}

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent(llm=FakeLLM())
    state = SessionState(session_id="sales-guided-semantic-1")

    for text in ("q promos tienes?", "hogar"):
        result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-guided-semantic-1",
                ),
                state,
            )
        )
        state.current_intent = result.intent

    semantic_reply = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="vivo solo pero se conectan como 4 equipos",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-semantic-1",
            ),
            state,
        )
    )

    lowered = (semantic_reply.message or "").lower()
    assert "pequeño, mediano o grande" in lowered or "pequeno, mediano o grande" in lowered
    assert state.metadata["sales"]["recommendation_profile"]["people"] == 1
    assert state.metadata["sales"]["recommendation_profile"]["devices"] == 4


def test_sales_agent_uses_contextual_interpreter_for_recommended_plan_followup(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    class FakeLLM:
        def enabled(self):
            return True

        async def extract_json(self, **kwargs):
            payload = (kwargs or {}).get("payload") or {}
            message = (payload.get("message") or {}).get("normalized")
            if "recommended_plan_context" in payload and message == "va ese entonces":
                return {"status": "ok", "result": {"decision": "accept"}}
            return {"status": "ok", "result": {}}

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent(llm=FakeLLM())
    state = SessionState(session_id="sales-guided-contextual-choice-1")

    steps = ("q promos tienes?", "hogar", "4 personas", "4 dispositivos", "mediano", "gaming")
    last_result = None
    for text in steps:
        last_result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-guided-contextual-choice-1",
                ),
                state,
            )
        )
        state.current_intent = last_result.intent

    assert last_result is not None
    assert "goprime" in last_result.message.lower()

    accepted = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="va ese entonces",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-contextual-choice-1",
            ),
            state,
        )
    )

    lowered = accepted.message.lower()
    assert accepted.intent == "commercial"
    assert "goprime" in lowered
    assert "nombre completo" in lowered
    assert state.metadata["sales"]["awaiting_crm_field"] == "partner_name"


def test_sales_agent_uses_active_turn_interpreter_for_recommended_plan_followup(monkeypatch):
    async def fake_fetch_catalog(self):
        return GUIDED_CATALOG

    class FakeLLM:
        def enabled(self):
            return True

        async def extract_json(self, **kwargs):
            payload = (kwargs or {}).get("payload") or {}
            flow_context = payload.get("flow_context") or {}
            message = (payload.get("message") or {}).get("normalized")
            if flow_context.get("current_stage") == "recommended_plan_followup" and message == "me late sigamos":
                return {
                    "status": "ok",
                    "result": {
                        "action": "accept_recommended_plan",
                        "confidence": 0.95,
                        "reason": "accept_in_context",
                    },
                }
            if (payload.get("recommended_plan_context") or {}) and message == "me late sigamos":
                return {"status": "ok", "result": {"decision": "accept"}}
            return {"status": "ok", "result": {}}

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    agent = SalesAgent(llm=FakeLLM())
    state = SessionState(session_id="sales-guided-active-followup-1")

    steps = ("q promos tienes?", "hogar", "4 personas", "4 dispositivos", "mediano", "gaming")
    last_result = None
    for text in steps:
        last_result = asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje=text,
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-guided-active-followup-1",
                ),
                state,
            )
        )
        state.current_intent = last_result.intent

    assert last_result is not None
    assert "goprime" in last_result.message.lower()

    accepted = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="me late sigamos",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-guided-active-followup-1",
            ),
            state,
        )
    )

    lowered = accepted.message.lower()
    assert accepted.intent == "commercial"
    assert "goprime" in lowered
    assert "nombre completo" in lowered
    assert state.metadata["sales"]["awaiting_crm_field"] == "partner_name"
