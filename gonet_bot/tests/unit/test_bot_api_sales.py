import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.bot_api.main import app
from packages.agents.sales.service import SalesAgent
from packages.integrations.agencies_repo import AgenciesRepo
from packages.integrations.geocoder import GeocoderClient
from packages.integrations.odoo_chat import OdooChatClient
from packages.integrations.odoo_crm import OdooCRMClient
from packages.integrations.promotions_api import PromotionsAPI
from packages.integrations.redis_store import build_session_store
from packages.shared.config import get_settings
from packages.shared.schemas import InboundMessage, SessionState


def test_sales_flow_keeps_session_and_advances_crm_capture():
    client = TestClient(app)
    session_id = "sales-seq-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero contratar un plan de internet",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert first.status_code == 200
    assert "nombre completo" in first.json()["message"].lower()

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Juan Perez",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    assert "ciudad" in second.json()["message"].lower()


def test_sales_flow_reuses_same_assistant_name_from_session(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "assistant_name", "Daniel")
    monkeypatch.setattr(settings, "assistant_names", "Daniel")

    client = TestClient(app)
    session_id = "sales-same-name-1"

    hello = client.post(
        "/v1/messages",
        json={
            "mensaje": "Hola",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert hello.status_code == 200
    hello_lowered = hello.json()["message"].lower()
    assert "daniel" in hello_lowered
    assert any(
        marker in hello_lowered
        for marker in ("te atiende", "soy ", "te saluda", "estás hablando con", "estas hablando con")
    )

    sales = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero información de planes",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert sales.status_code == 200
    body = sales.json()
    assert body["intent"] == "commercial"
    lowered = body["message"].lower()
    assert all(
        marker not in lowered
        for marker in (
            "hola, soy daniel",
            "te atiende daniel",
            "te saluda daniel",
            "estás hablando con daniel",
            "estas hablando con daniel",
        )
    )
    assert "andres" not in body["message"].lower()
    assert body["metadata"]["assistant_profile"]["display_name"] == "Daniel"


def test_sales_flow_answers_que_vendes_conversationally():
    client = TestClient(app)
    response = client.post(
        "/v1/messages",
        json={
            "mensaje": "¿Qué vendes?",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "sales-discovery-1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "sales"
    assert body["intent"] == "welcome"
    assert "planes hogar" in body["message"].lower()
    assert "agencias" in body["message"].lower()


def test_sales_flow_switches_to_agencies_when_user_changes_intent_during_crm_capture(monkeypatch):
    async def fake_by_city(self, city_upper: str):
        return []

    async def fake_by_province(self, province_upper: str):
        assert province_upper == "AZUAY"
        return [
            {
                "agencia": "Agencia Cuenca Centro",
                "ciudad": "Cuenca",
                "direccion": "Gran Colombia y Borrero",
                "horarios": "Lun a Vie 08:00 - 17:00",
                "telefono": "07-600-0001",
            }
        ]

    monkeypatch.setattr(AgenciesRepo, "by_city", fake_by_city)
    monkeypatch.setattr(AgenciesRepo, "by_province", fake_by_province)

    client = TestClient(app)
    session_id = "sales-to-agency-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Hola quisiera información de sus planes",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert first.status_code == 200
    assert first.json()["intent"] == "commercial"
    assert "nombre completo" in first.json()["message"].lower()

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "No, mejor deme las agencias del azuay",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body["agent"] == "sales"
    assert body["intent"] == "agencies"
    assert "agencias encontradas" in body["message"].lower()
    assert "cuenca" in body["message"].lower()
    assert "dirección completa" not in body["message"].lower()
    assert "direccion completa" not in body["message"].lower()

    state = asyncio.run(build_session_store().get(session_id))
    assert state is not None
    sales_state = (state.metadata or {}).get("sales") or {}
    assert sales_state.get("awaiting_crm_field") is None
    assert sales_state.get("pending_intent") is None


def test_sales_flow_prompts_info_choice_without_creating_crm_when_user_declines_registration(monkeypatch):
    crm_calls = []
    handoff_calls = []

    async def fake_fetch_catalog(self):
        return {
            "data": {
                "GONECTADOS": [
                    {"name": "Go Max", "mbps": "200", "price": "29.99", "details": [{"name": "Internet fibra"}]},
                    {"name": "Go Ultra", "mbps": "300", "price": "34.99", "details": [{"name": "Atención prioritaria"}]},
                    {"name": "Go Plus", "mbps": "120", "price": "24.99", "details": [{"name": "Internet fibra"}]},
                    {"name": "Go Light", "mbps": "80", "price": "19.99", "details": [{"name": "Internet fibra"}]},
                ],
                "PYMES": [
                    {"name": "Pyme 150", "mbps": "150", "price": "39.99", "details": [{"name": "Soporte empresarial"}]},
                ],
            }
        }

    async def fake_create_lead(self, payload):
        crm_calls.append(payload)
        return {"status": "created", "response": {"id": 1}}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoff_calls.append({"channel": channel, "recipient": recipient, "group": group})
        return {"status": "sent"}

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)
    monkeypatch.setattr(OdooCRMClient, "create_lead", fake_create_lead)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    response = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero ver los planes pero no quiero registrar mis datos",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "sales-no-crm-initial-1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "sales"
    assert body["intent"] == "commercial"
    assert "ver todos los planes" in body["message"].lower()
    assert "recomendación personalizada" in body["message"].lower() or "recomendacion personalizada" in body["message"].lower()
    assert "nombre completo" not in body["message"].lower()
    assert body["metadata"]["crm_lead_created"] is False
    assert body["metadata"]["commercial_registration_declined"] is True
    assert crm_calls == []
    assert handoff_calls == []

    state = asyncio.run(build_session_store().get("sales-no-crm-initial-1"))
    assert state is not None
    sales_state = (state.metadata or {}).get("sales") or {}
    assert sales_state.get("commercial_registration_declined") is True
    assert sales_state.get("awaiting_crm_field") is None
    assert sales_state.get("awaiting_info_choice") is True


def test_sales_flow_routes_from_info_choice_after_user_declines_registration_mid_capture(monkeypatch):
    crm_calls = []
    handoff_calls = []

    async def fake_fetch_catalog(self):
        return {
            "data": {
                "GONECTADOS": [
                    {"name": "Go Max", "mbps": "200", "price": "29.99", "details": [{"name": "Internet fibra"}]},
                    {"name": "Go Ultra", "mbps": "300", "price": "34.99", "details": [{"name": "Atención prioritaria"}]},
                    {"name": "Go Plus", "mbps": "120", "price": "24.99", "details": [{"name": "Internet fibra"}]},
                    {"name": "Go Light", "mbps": "80", "price": "19.99", "details": [{"name": "Internet fibra"}]},
                ],
                "PYMES": [
                    {"name": "Pyme 150", "mbps": "150", "price": "39.99", "details": [{"name": "Soporte empresarial"}]},
                    {"name": "Pyme 300", "mbps": "300", "price": "54.99", "details": [{"name": "IP pública opcional"}]},
                ],
            }
        }

    async def fake_create_lead(self, payload):
        crm_calls.append(payload)
        return {"status": "created", "response": {"id": 1}}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoff_calls.append({"channel": channel, "recipient": recipient, "group": group})
        return {"status": "sent"}

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)
    monkeypatch.setattr(OdooCRMClient, "create_lead", fake_create_lead)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "sales-no-crm-mid-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero contratar un plan de internet",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert first.status_code == 200
    assert "nombre completo" in first.json()["message"].lower()

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Prefiero no compartir mis datos, solo muéstrame los planes",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["intent"] == "commercial"
    assert "ver todos los planes" in second_body["message"].lower()
    assert "recomendación personalizada" in second_body["message"].lower() or "recomendacion personalizada" in second_body["message"].lower()
    assert "nombre completo" not in second_body["message"].lower()
    assert second_body["metadata"]["commercial_registration_declined"] is True

    third = client.post(
        "/v1/messages",
        json={
            "mensaje": "Muéstrame planes pymes",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert third.status_code == 200
    third_body = third.json()
    assert third_body["intent"] == "commercial"
    assert "estos son los planes" in third_body["message"].lower()
    assert "pymes" in third_body["message"].lower()
    assert "nombre completo" not in third_body["message"].lower()
    assert crm_calls == []
    assert handoff_calls == []

    state = asyncio.run(build_session_store().get(session_id))
    assert state is not None
    sales_state = (state.metadata or {}).get("sales") or {}
    assert sales_state.get("commercial_registration_declined") is True
    assert sales_state.get("awaiting_crm_field") is None


def test_sales_flow_treats_commercial_followup_as_catalog_request_during_crm_capture(monkeypatch):
    crm_calls = []
    handoff_calls = []

    async def fake_fetch_catalog(self):
        return {
            "data": {
                "GONECTADOS": [
                    {"name": "Go Max", "mbps": "200", "price": "29.99", "details": [{"name": "Internet fibra"}]},
                    {"name": "Go Ultra", "mbps": "300", "price": "34.99", "details": [{"name": "Atención prioritaria"}]},
                    {"name": "Go Plus", "mbps": "120", "price": "24.99", "details": [{"name": "Internet fibra"}]},
                    {"name": "Go Light", "mbps": "80", "price": "19.99", "details": [{"name": "Internet fibra"}]},
                ],
                "PYMES": [
                    {"name": "Pyme 150", "mbps": "150", "price": "39.99", "details": [{"name": "Soporte empresarial"}]},
                ],
            }
        }

    async def fake_create_lead(self, payload):
        crm_calls.append(payload)
        return {"status": "created", "response": {"id": 1}}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoff_calls.append({"channel": channel, "recipient": recipient, "group": group})
        return {"status": "sent"}

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)
    monkeypatch.setattr(OdooCRMClient, "create_lead", fake_create_lead)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "sales-commercial-followup-mid-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero contratar un plan de internet",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert first.status_code == 200
    assert "nombre completo" in first.json()["message"].lower()

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Y para hogar?",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body["agent"] == "sales"
    assert body["intent"] == "commercial"
    assert "sin registrar tus datos" in body["message"].lower()
    assert "planes residenciales" in body["message"].lower()
    assert "go max" in body["message"].lower()
    assert "nombre completo" not in body["message"].lower()
    assert body["metadata"]["crm_lead_created"] is False
    assert body["metadata"]["commercial_registration_declined"] is True
    assert crm_calls == []
    assert handoff_calls == []

    state = asyncio.run(build_session_store().get(session_id))
    assert state is not None
    sales_state = (state.metadata or {}).get("sales") or {}
    lead = sales_state.get("lead") or {}
    assert not lead.get("partner_name")
    assert sales_state.get("commercial_registration_declined") is True
    assert sales_state.get("awaiting_crm_field") is None


def test_sales_handle_escalates_to_human_for_operational_failures(monkeypatch):
    async def fail_reverse(self, latitude, longitude):
        raise httpx.ReadTimeout("geocoder timeout")

    handoffs = []

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
        return {"status": "sent", "channel_id": 7007}

    monkeypatch.setattr(GeocoderClient, "reverse", fail_reverse)

    agent = SalesAgent()
    monkeypatch.setattr(agent.handoff, "escalate_new_client", fake_escalate_new_client)
    result = asyncio.run(
        agent.handle(
            InboundMessage(
                mensaje="Necesito cobertura",
                channel="whatsapp",
                recipient="593999",
                session_id="sales-operational-error-1",
                location={"latitude": -2.17, "longitude": -79.92},
            ),
            SessionState(session_id="sales-operational-error-1"),
        )
    )

    assert result.agent == "handoff"
    assert result.intent == "human_handoff"
    assert "asesor especializado" in result.message.lower()
    assert handoffs[0]["group"] == "iainfo"
    assert "geocoder timeout" in (handoffs[0]["summary"] or "")


def test_sales_handle_surfaces_programming_errors(monkeypatch):
    async def broken_handle_internal(self, message, state):
        raise TypeError("unexpected bug")

    monkeypatch.setattr(SalesAgent, "_handle_internal", broken_handle_internal)

    agent = SalesAgent()
    with pytest.raises(TypeError, match="unexpected bug"):
        asyncio.run(
            agent.handle(
                InboundMessage(
                    mensaje="Hola",
                    channel="whatsapp",
                    recipient="593999",
                    session_id="sales-programming-error-1",
                ),
                SessionState(session_id="sales-programming-error-1"),
            )
        )


def test_sales_flow_keeps_commercial_context_for_generic_followup_without_registration(monkeypatch):
    async def fake_fetch_catalog(self):
        return {
            "data": {
                "GONECTADOS": [
                    {"name": "Go Max", "mbps": "200", "price": "29.99", "details": [{"name": "Internet fibra"}]},
                    {"name": "Go Ultra", "mbps": "300", "price": "34.99", "details": [{"name": "Atención prioritaria"}]},
                    {"name": "Go Plus", "mbps": "120", "price": "24.99", "details": [{"name": "Internet fibra"}]},
                    {"name": "Go Light", "mbps": "80", "price": "19.99", "details": [{"name": "Internet fibra"}]},
                ],
                "PYMES": [
                    {"name": "Pyme 150", "mbps": "150", "price": "39.99", "details": [{"name": "Soporte empresarial"}]},
                    {"name": "Pyme 300", "mbps": "300", "price": "54.99", "details": [{"name": "IP pública opcional"}]},
                ],
            }
        }

    monkeypatch.setattr(PromotionsAPI, "fetch_catalog", fake_fetch_catalog)

    client = TestClient(app)
    session_id = "sales-commercial-followup-info-only-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero ver los planes sin registrar mis datos",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["intent"] == "commercial"
    assert "sin registrar tus datos" in first_body["message"].lower()

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Y para hogar?",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["agent"] == "sales"
    assert second_body["intent"] == "commercial"
    assert "planes residenciales" in second_body["message"].lower()
    assert "go max" in second_body["message"].lower()
    assert "hola, te saluda" not in second_body["message"].lower()
    assert "nombre completo" not in second_body["message"].lower()

    third = client.post(
        "/v1/messages",
        json={
            "mensaje": "Y pymes?",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert third.status_code == 200
    third_body = third.json()
    assert third_body["agent"] == "sales"
    assert third_body["intent"] == "commercial"
    assert "planes pymes" in third_body["message"].lower()
    assert "pyme 300" in third_body["message"].lower()
    assert "hola, te saluda" not in third_body["message"].lower()
    assert "nombre completo" not in third_body["message"].lower()


def test_sales_flow_accepts_written_address_geocoding_in_mock_mode(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mock_mode", True)

    client = TestClient(app)
    session_id = "sales-coords-1"

    steps = [
        {"mensaje": "Quiero contratar internet", "channel": "whatsapp", "recipient": "593999", "session_id": session_id},
        {"mensaje": "Juan Perez", "channel": "whatsapp", "recipient": "593999", "session_id": session_id},
        {"mensaje": "Guayaquil", "channel": "whatsapp", "recipient": "593999", "session_id": session_id},
        {"mensaje": "Av. Demo 123 y Primera", "channel": "whatsapp", "recipient": "593999", "session_id": session_id},
        {"mensaje": "0999999999", "channel": "whatsapp", "recipient": "593999", "session_id": session_id},
    ]

    body = None
    for payload in steps:
        response = client.post("/v1/messages", json=payload)
        assert response.status_code == 200
        body = response.json()

    assert body is not None
    assert body["intent"] == "commercial"
    assert "planes" in body["message"].lower()
    assert "ubicación exacta" not in body["message"].lower()
    assert "ubicacion exacta" not in body["message"].lower()


def test_sales_flow_agencies_asks_for_location_when_missing():
    client = TestClient(app)
    response = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero saber dónde queda una agencia",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "agency-1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "sales"
    assert body["intent"] == "agencies"
    assert "ciudad o provincia" in body["message"].lower()


def test_sales_flow_keeps_agencies_context_for_followup_location(monkeypatch):
    async def fake_by_city(self, city_upper: str):
        if city_upper == "CUENCA":
            return [
                {
                    "agencia": "Agencia Cuenca",
                    "ciudad": "Cuenca",
                    "direccion": "Centro",
                    "horarios": "08:00 - 17:00",
                    "telefono": "07-600-0001",
                }
            ]
        return []

    async def fake_by_province(self, province_upper: str):
        if province_upper == "AZUAY":
            return [
                {
                    "agencia": "Agencia Azuay",
                    "ciudad": "Cuenca",
                    "direccion": "Av. Demo Azuay",
                    "horarios": "08:00 - 17:00",
                    "telefono": "07-600-0001",
                }
            ]
        if province_upper == "LOJA":
            return [
                {
                    "agencia": "Agencia Loja",
                    "ciudad": "Loja",
                    "direccion": "Av. Demo Loja",
                    "horarios": "08:00 - 17:00",
                    "telefono": "07-600-0002",
                }
            ]
        return []

    monkeypatch.setattr(AgenciesRepo, "by_city", fake_by_city)
    monkeypatch.setattr(AgenciesRepo, "by_province", fake_by_province)

    client = TestClient(app)
    session_id = "agency-followup-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero saber las agencias del azuay",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["agent"] == "sales"
    assert first_body["intent"] == "agencies"
    assert "agencia azuay" in first_body["message"].lower()
    assert "teléfono" not in first_body["message"].lower()
    assert "telefono" not in first_body["message"].lower()

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Y de Loja?",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["agent"] == "sales"
    assert second_body["intent"] == "agencies"
    assert "agencia loja" in second_body["message"].lower()
    assert "teléfono" not in second_body["message"].lower()
    assert "telefono" not in second_body["message"].lower()
    lowered = second_body["message"].lower()
    assert not any(
        marker in lowered
        for marker in ("hola, soy ", "te atiende ", "te saluda ", "estás hablando con ", "estas hablando con ")
    )
