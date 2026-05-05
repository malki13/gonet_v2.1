import asyncio

from packages.agents.sales.service import SalesAgent
from packages.agents.sales.utils import _extract_coordinates_from_text
from packages.shared.schemas import InboundMessage, SessionState


def test_sales_next_crm_field_requests_city_before_street():
    agent = SalesAgent()
    state = SessionState(session_id="sales-location-order-1")
    sales_state = agent._get_sales_state(state)
    sales_state.setdefault("lead", {})["partner_name"] = "Juan Perez"

    assert agent._next_crm_field(sales_state) == "city"


def test_sales_ingests_precise_written_address_with_forward_geocoding(monkeypatch):
    agent = SalesAgent()
    state = SessionState(session_id="sales-location-1")
    sales_state = agent._get_sales_state(state)
    sales_state.setdefault("lead", {})["city"] = "Guayaquil"

    async def fake_forward(query, *, city=None, province=None):
        assert "garcia moreno" in query.lower()
        assert city == "Guayaquil"
        return {
            "latitude": -2.1961,
            "longitude": -79.8862,
            "city": "Guayaquil",
            "province": "Guayas",
            "zone": "Centro",
            "address": "Av. Garcia Moreno y Republica, Guayaquil, Guayas, Ecuador",
        }

    monkeypatch.setattr(agent.geocoder, "forward", fake_forward)

    changed = asyncio.run(agent._ingest_text_location(sales_state, "Av Garcia Moreno y Republica"))

    assert changed is True
    lead = sales_state["lead"]
    assert lead["latitude"] == -2.1961
    assert lead["longitude"] == -79.8862
    assert lead["city"] == "Guayaquil"
    assert lead["province"] == "Guayas"


def test_sales_does_not_treat_city_only_as_precise_address(monkeypatch):
    agent = SalesAgent()
    state = SessionState(session_id="sales-location-2")
    sales_state = agent._get_sales_state(state)

    async def should_not_call(*args, **kwargs):
        raise AssertionError("forward geocoder should not be called for city-only input")

    monkeypatch.setattr(agent.geocoder, "forward", should_not_call)

    changed = asyncio.run(agent._ingest_text_location(sales_state, "Guayaquil"))

    assert changed is True
    lead = sales_state["lead"]
    assert lead["city"] == "Guayaquil"
    assert lead.get("latitude") is None
    assert lead.get("longitude") is None


def test_extract_coordinates_from_google_maps_search_url_with_plus_separator():
    result = _extract_coordinates_from_text(
        "https://www.google.com/maps/search/-4.003688,+-79.202511?entry=tts"
    )

    assert result == {"latitude": -4.003688, "longitude": -79.202511}


def test_extract_coordinates_from_url_preserves_negative_latitude():
    result = _extract_coordinates_from_text(
        "https://www.google.com/maps/search/-4.003688,-79.202511?entry=tts"
    )

    assert result == {"latitude": -4.003688, "longitude": -79.202511}


def test_sales_city_capture_accepts_homonymous_province_name():
    agent = SalesAgent()
    state = SessionState(session_id="sales-location-2b")
    sales_state = agent._get_sales_state(state)
    sales_state.setdefault("lead", {})["partner_name"] = "Fernando Cordero"

    changed = asyncio.run(agent._ingest_text_location(sales_state, "Loja"))

    assert changed is True
    assert agent._save_crm_answer(sales_state, "city", "Loja") is True

    lead = sales_state["lead"]
    assert lead["city"] == "Loja"
    assert lead["province"] == "Loja"
    assert agent._next_crm_field(sales_state) == "street"


def test_sales_city_capture_rejects_street_like_input(monkeypatch):
    agent = SalesAgent()
    state = SessionState(session_id="sales-location-city-streetlike-1")
    sales_state = agent._get_sales_state(state)
    sales_state.setdefault("lead", {})["partner_name"] = "Freddy Cabrera"
    sales_state["pending_intent"] = "commercial"
    sales_state["awaiting_crm_field"] = "city"
    state.current_intent = "commercial"

    async def fake_by_city(city_upper: str):
        raise AssertionError("city lookup should not run for street-like input")

    monkeypatch.setattr(agent.agencies, "by_city", fake_by_city)

    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="Av Siempre Viva",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-location-city-streetlike-1",
            ),
            state,
        )
    )

    lowered = result.message.lower()
    assert "suena más a dirección" in lowered or "suena mas a direccion" in lowered
    assert result.metadata["response_plan"]["conversation_state"] == "crm_city_needs_city_not_street"
    assert sales_state.get("lead", {}).get("city") is None
    assert sales_state["awaiting_crm_field"] == "city"


def test_sales_city_capture_rejects_unsupported_city(monkeypatch):
    agent = SalesAgent()
    state = SessionState(session_id="sales-location-city-unsupported-1")
    sales_state = agent._get_sales_state(state)
    sales_state.setdefault("lead", {})["partner_name"] = "Freddy Cabrera"
    sales_state["pending_intent"] = "commercial"
    sales_state["awaiting_crm_field"] = "city"
    state.current_intent = "commercial"

    async def fake_by_city(city_upper: str):
        assert city_upper == "QUITO"
        return []

    monkeypatch.setattr(agent.agencies, "by_city", fake_by_city)

    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="Quito",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-location-city-unsupported-1",
            ),
            state,
        )
    )

    lowered = result.message.lower()
    assert "no veo cobertura en esa ciudad" in lowered
    assert result.metadata["response_plan"]["conversation_state"] == "crm_city_unsupported"
    assert sales_state.get("lead", {}).get("city") is None
    assert sales_state["awaiting_crm_field"] == "city"


def test_sales_city_capture_accepts_supported_city_and_moves_to_street(monkeypatch):
    agent = SalesAgent()
    state = SessionState(session_id="sales-location-city-supported-1")
    sales_state = agent._get_sales_state(state)
    sales_state.setdefault("lead", {})["partner_name"] = "Freddy Cabrera"
    sales_state["pending_intent"] = "commercial"
    sales_state["awaiting_crm_field"] = "city"
    state.current_intent = "commercial"

    async def fake_by_city(city_upper: str):
        assert city_upper == "LOJA"
        return [{"ciudad": "Loja", "provincia": "Loja"}]

    monkeypatch.setattr(agent.agencies, "by_city", fake_by_city)

    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="Loja",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-location-city-supported-1",
            ),
            state,
        )
    )

    lowered = result.message.lower()
    assert "direccion donde seria la instalacion" in lowered or "dirección donde sería la instalación" in lowered
    assert sales_state["lead"]["city"] == "Loja"
    assert sales_state["awaiting_crm_field"] == "street"


def test_sales_skips_coordinates_step_when_written_address_is_validated(monkeypatch):
    agent = SalesAgent()
    state = SessionState(session_id="sales-location-3")
    sales_state = agent._get_sales_state(state)
    lead = sales_state.setdefault("lead", {})
    lead["partner_name"] = "Juan Perez"
    lead["city"] = "Guayaquil"
    lead["phone"] = "0999999999"

    async def fake_forward(query, *, city=None, province=None):
        return {
            "latitude": -2.1961,
            "longitude": -79.8862,
            "city": "Guayaquil",
            "province": "Guayas",
            "zone": "Centro",
            "address": "Av. Garcia Moreno y Republica, Guayaquil, Guayas, Ecuador",
        }

    monkeypatch.setattr(agent.geocoder, "forward", fake_forward)

    asyncio.run(agent._ingest_text_location(sales_state, "Av Garcia Moreno y Republica"))
    lead["street"] = "Av Garcia Moreno y Republica"

    assert agent._next_crm_field(sales_state) is None


def test_invalid_street_does_not_overwrite_city_or_fake_location(monkeypatch):
    agent = SalesAgent()
    state = SessionState(session_id="sales-location-4")
    sales_state = agent._get_sales_state(state)
    sales_state.setdefault("lead", {})["city"] = "Cuenca"

    async def fake_forward(query, *, city=None, province=None):
        assert query == "Av siempre viva y Cuello"
        assert city == "Cuenca"
        return {}

    monkeypatch.setattr(agent.geocoder, "forward", fake_forward)

    changed = asyncio.run(
        agent._ingest_text_location(
            sales_state,
            "Av siempre viva y Cuello",
            allow_general_location=False,
        )
    )

    assert changed is False
    lead = sales_state["lead"]
    assert lead["city"] == "Cuenca"
    assert lead.get("address") is None
    assert lead.get("latitude") is None
    assert lead.get("longitude") is None


def test_invalid_street_uses_conversational_location_fallback():
    agent = SalesAgent()
    state = SessionState(session_id="sales-location-5")
    sales_state = agent._get_sales_state(state)
    lead = sales_state.setdefault("lead", {})
    lead["partner_name"] = "Pedro Picapiedra"
    lead["city"] = "Cuenca"
    lead["street"] = "Av siempre viva y Cuello"
    lead["phone"] = "0999999999"

    result = asyncio.run(
        agent._continue_crm_capture(
            sales_state=sales_state,
            user_message="Av siempre viva y Cuello",
            channel="whatsapp",
            recipient="0999999999",
            cedula=None,
        )
    )

    assert "no pude ubicar esa dirección con seguridad" in result.message.lower()


def test_sales_final_capture_does_not_append_catalog_after_registration(monkeypatch):
    agent = SalesAgent()
    state = SessionState(session_id="sales-location-finalize-1")
    sales_state = agent._get_sales_state(state)
    crm_calls = []
    sales_state.update(
        {
            "commercial_catalog_requested": True,
            "recommended_plan": {"name": "GoEssencial", "segment": "residential"},
        }
    )
    lead = sales_state.setdefault("lead", {})
    lead.update(
        {
            "partner_name": "Freddy Cabrera",
            "city": "Loja",
            "street": "Av Siempre Viva",
            "address": "Calle Real 123, Loja, Ecuador",
            "phone": "0999999999",
            "latitude": -4.003688,
            "longitude": -79.202511,
        }
    )

    async def fake_create_lead(payload):
        crm_calls.append(payload)
        return {"status": "created", "response": {"id": 1}}

    async def fake_handoff(*, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        return {"status": "sent"}

    async def should_not_fetch_catalog(*args, **kwargs):
        raise AssertionError("catalog should not be fetched after final registration")

    monkeypatch.setattr(agent.crm, "create_lead", fake_create_lead)
    monkeypatch.setattr(agent.handoff, "escalate_new_client", fake_handoff)
    monkeypatch.setattr(agent.promotions, "fetch_catalog", should_not_fetch_catalog)

    result = asyncio.run(
        agent._continue_crm_capture(
            sales_state=sales_state,
            user_message="https://maps.app.goo.gl/example",
            channel="whatsapp",
            recipient="593999",
            cedula=None,
        )
        )

    lowered = result.message.lower()
    assert "goessencial" in lowered
    assert "registrada su solicitud" in lowered
    assert "estos son los planes" not in lowered
    assert "planes hogar disponibles" not in lowered
    assert crm_calls[0]["street"] == lead["address"]
    assert crm_calls[0]["latitude"] == -4.003688
    assert crm_calls[0]["longitude"] == -79.202511
