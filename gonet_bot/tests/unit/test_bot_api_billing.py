import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from apps.bot_api.main import app
from packages.agents.billing_async import BillingAsyncProcessor
from packages.channels.delivery import ChannelDeliveryService
from packages.integrations.billing_registration import BillingRegistrationService
from packages.integrations.contract_lookup import ContractLookupClient
from packages.integrations.contact_registry import build_contact_registry
from packages.integrations.ocr_queue import OCRJobQueue
from packages.integrations.ocr_service_client import OCRServiceClient
from packages.integrations.odoo_chat import OdooChatClient
from packages.integrations.redis_store import build_session_store
from packages.orchestrator.session_context import SessionContextService
from packages.shared.config import get_settings
from packages.shared.errors import OCRQueueUnavailableError
from packages.shared.schemas import InboundMessage, OCRJob, SessionState
from tests.unit.bot_api_helpers import _accept_consent


def test_billing_flow_keeps_cedula_and_domain_after_identity_resolution(monkeypatch):
    async def fake_info(self, cedula: str):
        assert cedula == "0703042374001"
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-002",
                    "state": "cortado",
                    "partner_name": "Juan Perez",
                    "residual": "51.75",
                }
            ],
        }

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    client = TestClient(app)
    session_id = "billing-cedula-memory-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "No tengo internet",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert first.status_code == 200
    assert first.json()["intent"] == "ask_cedula"

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "0703042374001",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["agent"] == "clarify"
    assert second_body["intent"] == "consent_required"

    third = _accept_consent(client, session_id=session_id)
    assert third.status_code == 200
    third_body = third.json()
    assert third_body["agent"] == "billing"
    assert third_body["intent"] == "billing"
    assert "link de cobro" in third_body["message"].lower()

    state = asyncio.run(
        SessionContextService().load(
            InboundMessage(
                mensaje="",
                channel="whatsapp",
                recipient="593999",
                session_id=session_id,
            )
        )
    )
    assert state.cedula == "0703042374001"
    assert state.current_intent == "billing"
    assert state.last_agent == "billing"

    fourth = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero registrar mi pago",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert fourth.status_code == 200
    fourth_body = fourth.json()
    assert fourth_body["agent"] == "billing"
    assert fourth_body["intent"] == "billing_proof_requested"
    assert "comprobante" in fourth_body["message"].lower()
    assert fourth_body["intent"] != "ask_cedula"


def test_billing_flow_supports_contract_selection_and_payment_link(monkeypatch):
    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-001",
                    "state": "active",
                    "partner_name": "Juan Perez",
                },
                {
                    "code": "CT-002",
                    "state": "cortado",
                    "partner_name": "Juan Perez",
                    "residual": "21.50",
                },
            ],
        }

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    client = TestClient(app)
    session_id = "billing-select-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Necesito ayuda con facturación",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert first.status_code == 200
    assert first.json()["intent"] == "contract_selection"
    assert "Juan Perez" in first.json()["message"]

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "2",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["intent"] == "consent_required"
    assert "Juan Perez" in second_body["message"]

    third = _accept_consent(client, session_id=session_id)
    assert third.status_code == 200
    assert third.json()["agent"] == "billing"
    assert "Juan Perez" in third.json()["message"]
    assert "link de cobro" in third.json()["message"].lower()

    fourth = client.post(
        "/v1/messages",
        json={
            "mensaje": "Link de Cobro",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert fourth.status_code == 200
    body = fourth.json()
    assert body["agent"] == "billing"
    assert "https://pagos.gonet.ec/payment" in body["message"]
    assert "/0102030405" not in body["message"]


def test_billing_flow_shows_debt_and_options_after_consent_for_register_payment_request(monkeypatch):
    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-REG-1",
                    "state": "cortado",
                    "partner_name": "Juan Perez",
                    "residual": "48.75",
                }
            ],
        }

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    client = TestClient(app)
    session_id = "billing-register-options-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero registrar mi pago",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert first.status_code == 200
    assert first.json()["intent"] == "consent_required"

    accepted = _accept_consent(client, session_id=session_id)
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["agent"] == "billing"
    assert body["intent"] == "billing"
    assert "48.75" in body["message"]
    assert "registrar pago" in body["message"].lower()
    assert "link de cobro" in body["message"].lower()


def test_billing_flow_interprets_ya_pague_as_proof_request(monkeypatch):
    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-PAID-1",
                    "state": "cortado",
                    "partner_name": "Juan Perez",
                    "residual": "33.25",
                }
            ],
        }

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    client = TestClient(app)
    session_id = "billing-ya-pague-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Necesito facturación",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert first.status_code == 200
    assert first.json()["intent"] == "consent_required"

    accepted = _accept_consent(client, session_id=session_id)
    assert accepted.status_code == 200
    assert accepted.json()["agent"] == "billing"

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Ya pagué",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["intent"] == "billing_proof_requested"
    assert "comprobante" in second_body["message"].lower()


def test_billing_flow_processes_proof_with_ocr_service(monkeypatch):
    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-009",
                    "state": "cortado",
                    "partner_name": "Juan Perez",
                    "residual": "18.00",
                }
            ],
        }

    async def fake_analyze(self, attachment, *, notify_gonet_bot: bool = False):
        return {
            "estado": "validated",
            "debe_reintentar": False,
            "texto_extraido": "Comprobante válido",
            "raw": {"estado": "ok"},
        }

    async def fake_register(self, *, contract, ocr_result, attachments, cedula):
        return {
            "status": "created",
            "deposit_id": 840,
            "resolved": {
                "code": "130283",
                "value": 18.00,
                "pending_value": 18.00,
                "balance_due": 0.0,
                "reconnect_status": "done",
                "deposit": {"name": "BANCO PICHINCHA"},
            },
        }

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(OCRServiceClient, "enabled", property(lambda self: True))
    monkeypatch.setattr(OCRServiceClient, "analyze", fake_analyze)
    monkeypatch.setattr(BillingRegistrationService, "register_payment", fake_register)

    client = TestClient(app)
    session_id = "billing-ocr-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Necesito facturación",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert first.status_code == 200
    assert first.json()["intent"] == "consent_required"

    accepted = _accept_consent(client, session_id=session_id)
    assert accepted.status_code == 200
    assert accepted.json()["agent"] == "billing"

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Registrar Pago",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert second.status_code == 200
    assert second.json()["intent"] == "billing_proof_requested"

    third = client.post(
        "/v1/messages",
        json={
            "mensaje": "Adjunto comprobante",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
            "attachments": [
                {
                    "filename": "proof.png",
                    "mime_type": "image/png",
                    "base64_data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
                }
            ],
        },
    )
    assert third.status_code == 200
    body = third.json()
    assert body["agent"] == "billing"
    assert body["intent"] == "billing_payment_registered_reconnected"
    assert "dejar el servicio reconectado" in body["message"].lower()


def test_billing_flow_reports_reconnect_error_after_registering_payment(monkeypatch):
    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-010",
                    "state": "cortado",
                    "partner_name": "Juan Perez",
                    "residual": "18.00",
                }
            ],
        }

    async def fake_analyze(self, attachment, *, notify_gonet_bot: bool = False):
        return {
            "estado": "validated",
            "debe_reintentar": False,
            "texto_extraido": "Comprobante válido",
            "raw": {"estado": "ok"},
        }

    async def fake_register(self, *, contract, ocr_result, attachments, cedula):
        return {
            "status": "created",
            "deposit_id": 841,
            "resolved": {
                "code": "130284",
                "value": 18.00,
                "pending_value": 18.00,
                "balance_due": 0.0,
                "reconnect_status": "error",
                "reconnect_error": "franchise_aes_key_not_configured",
                "deposit": {"name": "BANCO PICHINCHA"},
            },
        }

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(OCRServiceClient, "enabled", property(lambda self: True))
    monkeypatch.setattr(OCRServiceClient, "analyze", fake_analyze)
    monkeypatch.setattr(BillingRegistrationService, "register_payment", fake_register)

    client = TestClient(app)
    session_id = "billing-ocr-reconnect-error-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Necesito facturación",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert first.status_code == 200
    assert first.json()["intent"] == "consent_required"

    accepted = _accept_consent(client, session_id=session_id)
    assert accepted.status_code == 200
    assert accepted.json()["agent"] == "billing"

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Registrar Pago",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert second.status_code == 200
    assert second.json()["intent"] == "billing_proof_requested"

    third = client.post(
        "/v1/messages",
        json={
            "mensaje": "Adjunto comprobante",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
            "attachments": [
                {
                    "filename": "proof.png",
                    "mime_type": "image/png",
                    "base64_data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
                }
            ],
        },
    )
    assert third.status_code == 200
    body = third.json()
    assert body["agent"] == "billing"
    assert body["intent"] == "billing_payment_registered"
    assert "no pude completar la reconexión automática" in body["message"].lower()


def test_billing_flow_queues_proof_when_async_ocr_enabled(monkeypatch):
    queued_jobs = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-ASYNC",
                    "state": "cortado",
                    "partner_name": "Juan Perez",
                    "residual": "25.00",
                }
            ],
        }

    async def fake_enqueue(self, job):
        queued_jobs.append(job)
        return {"ok": True, "backend": "memory", "size": len(queued_jobs)}

    settings = get_settings()
    monkeypatch.setattr(settings, "ocr_async_enabled", True)
    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(OCRJobQueue, "enqueue", fake_enqueue)

    client = TestClient(app)
    session_id = "billing-async-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Necesito facturación",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert first.status_code == 200
    assert first.json()["intent"] == "consent_required"

    accepted = _accept_consent(client, session_id=session_id)
    assert accepted.status_code == 200
    assert accepted.json()["agent"] == "billing"

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Registrar Pago",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert second.status_code == 200
    assert second.json()["intent"] == "billing_proof_requested"

    third = client.post(
        "/v1/messages",
        json={
            "mensaje": "Adjunto comprobante",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
            "attachments": [
                {
                    "filename": "proof.png",
                    "mime_type": "image/png",
                    "base64_data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
                }
            ],
        },
    )
    assert third.status_code == 200
    body = third.json()
    assert body["agent"] == "billing"
    assert body["intent"] == "billing_proof_queued"
    assert "ya lo envié a validación" in body["message"].lower()
    assert len(queued_jobs) == 1
    assert queued_jobs[0].contract["code"] == "CT-ASYNC"

    fourth = client.post(
        "/v1/messages",
        json={
            "mensaje": "¿Ya revisaron mi comprobante?",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert fourth.status_code == 200
    body = fourth.json()
    assert body["intent"] == "billing_processing_async"
    assert body["message"] == ""
    assert body["metadata"]["skip_delivery"] is True


def test_contract_lookup_uses_mock_mode_when_not_configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mock_mode", True)
    monkeypatch.setattr(ContractLookupClient().client, "is_configured", lambda: False)

    client = ContractLookupClient()
    monkeypatch.setattr(client.client, "is_configured", lambda: False)

    result = asyncio.run(client.info_personal_by_cedula("0102030405"))
    assert result["ok"] is True
    assert len(result["data"]) == 2
    assert result["data"][0]["code"].startswith("ACT-")
    assert result["data"][1]["state"] == "cortado"


def test_contract_lookup_confirms_empty_result_with_final_regular_retry(monkeypatch):
    client = ContractLookupClient()
    monkeypatch.setattr(client.client, "is_configured", lambda: True)

    calls = []
    queued_results = {
        (False, False): [[], [], [], [{"code": "901173", "state": "open"}]],
        (True, True): [[]],
    }

    async def fake_fetch(self, cedula: str, *, refresh_franchises: bool, persist_franchises: bool):
        calls.append((cedula, refresh_franchises, persist_franchises))
        return queued_results[(refresh_franchises, persist_franchises)].pop(0)

    async def fake_sleep(_: float):
        return None

    monkeypatch.setattr(ContractLookupClient, "_fetch_contract_lookup", fake_fetch)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = asyncio.run(client.info_personal_by_cedula("0707017315"))

    assert result["ok"] is True
    assert result["data"] == [{"code": "901173", "state": "open"}]
    assert calls == [
        ("0707017315", False, False),
        ("0707017315", False, False),
        ("0707017315", False, False),
        ("0707017315", True, True),
        ("0707017315", False, False),
    ]


def test_contract_lookup_uses_contact_center_fallback_when_odoo_returns_empty(monkeypatch):
    client = ContractLookupClient()
    settings = get_settings()
    monkeypatch.setattr(settings, "contact_center_lookup_url", "http://contact-center.test")
    monkeypatch.setattr(client.client, "is_configured", lambda: True)

    async def fake_fetch(self, cedula: str, *, refresh_franchises: bool, persist_franchises: bool):
        return []

    async def fake_lookup(self, texto: str):
        assert texto == "0907938674"
        return {
            "results": [
                {
                    "cliente_identificacion": "0907938674",
                    "cliente_nombre": "FERNANDEZ TROYA MANUEL HIGINIO - 0907938674",
                    "cliente_email": "manuel@example.com",
                    "celular": "0960953520",
                    "compania_nombre": "MARVICNET CIA LTDA",
                    "contrato_codigo": "200462",
                    "contrato_estado": "CORTADO",
                    "localizacion_provincia": "El Oro (EC)",
                    "localizacion_ciudad": "Santa Rosa",
                    "localizacion_direccion": "volter cordova y teresa arcaya",
                    "localizacion_referencia": "CERRAMIENTO PINTALO DE AZUL",
                    "localizacion_sector": "D07-TENIENTE HUGO ORTIZ",
                    "plan_activo": "PRE-24/GOESSENCIAL-600/SIN PROMO",
                    "precio_plan": 22.89,
                },
                {
                    "cliente_identificacion": "0907938674",
                    "cliente_nombre": "FERNANDEZ TROYA MANUEL HIGINIO - 0907938674",
                    "cliente_email": "S/N",
                    "celular": "S/N",
                    "compania_nombre": "MARVICNET CIA LTDA",
                    "contrato_codigo": "200462",
                    "contrato_estado": "S/N",
                    "plan_activo": "GOESSENCIAL 600 MBPS",
                    "precio_plan": 19.90,
                },
            ]
        }

    async def fake_sleep(_: float):
        return None

    monkeypatch.setattr(ContractLookupClient, "_fetch_contract_lookup", fake_fetch)
    monkeypatch.setattr(ContractLookupClient, "_fetch_contact_center_lookup", fake_lookup)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = asyncio.run(client.info_personal_by_cedula("0907938674"))

    assert result["ok"] is True
    assert result["source"] == "contact_center"
    assert len(result["data"]) == 1
    contract = result["data"][0]
    assert contract["code"] == "200462"
    assert contract["state"] == "cortado"
    assert contract["residual"] == "22.89"
    assert contract["partner"]["dni"] == "0907938674"
    assert contract["partner"]["email"] == "manuel@example.com"
    assert "Santa Rosa" in contract["street"]


def test_contract_lookup_get_email_uses_contact_center_fallback(monkeypatch):
    client = ContractLookupClient()
    settings = get_settings()
    monkeypatch.setattr(settings, "contact_center_lookup_url", "http://contact-center.test")
    monkeypatch.setattr(client.client, "is_configured", lambda: True)

    async def fake_execute_kw(model: str, method: str, args=None, kwargs=None):
        return []

    async def fake_lookup(self, texto: str):
        assert texto == "0907938674"
        return {
            "results": [
                {
                    "cliente_identificacion": "0907938674",
                    "cliente_nombre": "FERNANDEZ TROYA MANUEL HIGINIO - 0907938674",
                    "cliente_email": "manuel@example.com",
                    "contrato_codigo": "200462",
                    "contrato_estado": "CORTADO",
                    "precio_plan": 22.89,
                }
            ]
        }

    monkeypatch.setattr(client.client, "execute_kw", fake_execute_kw)
    monkeypatch.setattr(ContractLookupClient, "_fetch_contact_center_lookup", fake_lookup)

    email = asyncio.run(client.get_email_by_cedula("0907938674"))

    assert email == "manuel@example.com"


def test_billing_flow_uses_contact_center_fallback_when_odoo_returns_empty(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "contact_center_lookup_url", "http://contact-center.test")

    async def fake_fetch(self, cedula: str, *, refresh_franchises: bool, persist_franchises: bool):
        assert cedula == "0907938674"
        return []

    async def fake_lookup(self, texto: str):
        assert texto == "0907938674"
        return {
            "results": [
                {
                    "cliente_identificacion": "0907938674",
                    "cliente_nombre": "FERNANDEZ TROYA MANUEL HIGINIO - 0907938674",
                    "cliente_email": "manuel@example.com",
                    "celular": "0960953520",
                    "compania_nombre": "MARVICNET CIA LTDA",
                    "contrato_codigo": "200462",
                    "contrato_estado": "CORTADO",
                    "localizacion_provincia": "El Oro (EC)",
                    "localizacion_ciudad": "Santa Rosa",
                    "localizacion_direccion": "volter cordova y teresa arcaya",
                    "plan_activo": "PRE-24/GOESSENCIAL-600/SIN PROMO",
                    "precio_plan": 22.89,
                }
            ]
        }

    async def fake_sleep(_: float):
        return None

    monkeypatch.setattr(ContractLookupClient, "_fetch_contract_lookup", fake_fetch)
    monkeypatch.setattr(ContractLookupClient, "_fetch_contact_center_lookup", fake_lookup)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    client = TestClient(app)
    session_id = "billing-contact-center-fallback-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero registrar mi pago",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert first.status_code == 200
    assert first.json()["intent"] == "ask_cedula"

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "0907938674",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    assert second.json()["intent"] == "consent_required"

    third = _accept_consent(client, session_id=session_id)
    assert third.status_code == 200
    body = third.json()
    assert body["agent"] == "billing"
    assert body["intent"] == "billing"
    assert "200462" in body["message"]


def test_billing_async_processor_sends_final_message_in_mock_mode(monkeypatch):
    sent_messages = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        sent_messages.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "actions": actions,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    async def fake_analyze(self, attachment, *, notify_gonet_bot: bool = False):
        return {
            "estado": "validated",
            "debe_reintentar": False,
            "texto_extraido": "Comprobante válido",
            "raw": {"estado": "ok"},
        }

    async def fake_register(self, *, contract, ocr_result, attachments, cedula):
        return {
            "status": "created",
            "resolved": {
                "reconnect_status": "done",
            },
        }

    settings = get_settings()
    monkeypatch.setattr(settings, "mock_mode", True)
    monkeypatch.setattr(OCRServiceClient, "enabled", property(lambda self: True))
    monkeypatch.setattr(OCRServiceClient, "analyze", fake_analyze)
    monkeypatch.setattr(BillingRegistrationService, "register_payment", fake_register)
    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)

    processor = BillingAsyncProcessor()
    job = OCRJob(
        job_id="job-mock-1",
        session_id="session-mock-1",
        channel="whatsapp",
        recipient="593999",
        cedula="0102030405",
        contract={"code": "CUT-0405", "residual": "18.50"},
        attachments=[{"filename": "proof.png", "mime_type": "image/png", "base64_data": "abc123"}],
        message="Adjunto comprobante",
    )

    result = asyncio.run(processor.process(job))
    assert result["status"] == "created"
    assert len(sent_messages) == 1
    assert sent_messages[0]["recipient"] == "593999"
    assert "validar el comprobante" in sent_messages[0]["message"].lower()


def test_billing_async_processor_creates_payment_for_contact_center_contract_without_handoff(monkeypatch):
    sent_messages = []
    handoffs = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        sent_messages.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "actions": actions,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
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

    async def fake_find_local_partner_id(self, dni: str | None):
        return None

    async def fake_find_existing_deposit(self, code: str | None):
        return None

    async def fake_fetch_pending_invoices(self, *, franchise_id: int, partner_invoice_id: int, contract_id: int):
        raise AssertionError("contact_center fallback should not request pending invoices")

    async def fake_execute(self, model: str, method: str, *method_args):
        if method == "create":
            return 840
        return True

    async def fake_execute_kw(self, model: str, method: str, *, args=None, kwargs=None):
        if model == "app.gonet.franchise" and method == "search_read":
            return [{"id": 1, "name": "MARVICNET CIA. LTDA.", "code": 5}]
        if model == "app.gonet.franchise.deposit" and method == "search_read":
            return [
                {
                    "id": 1,
                    "name": "BANCO PICHINCHA Cta. Cte. # 2100174952",
                    "code": "BPCH",
                    "number": "2100174952",
                    "is_collection": False,
                }
            ]
        return []

    async def fake_reconnect_with_tolerance(
        self,
        *,
        deposit_id: int,
        local_contract_record_id: int | None,
        local_contract_json: str | None,
        resolved: dict,
    ):
        return {"id": 220229, "model": "sale.subscription"}

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)
    monkeypatch.setattr(BillingRegistrationService, "_is_configured", lambda self: True)
    monkeypatch.setattr(BillingRegistrationService, "_find_local_partner_id", fake_find_local_partner_id)
    monkeypatch.setattr(BillingRegistrationService, "_find_existing_deposit", fake_find_existing_deposit)
    monkeypatch.setattr(BillingRegistrationService, "_fetch_pending_invoices", fake_fetch_pending_invoices)
    monkeypatch.setattr(BillingRegistrationService, "_execute", fake_execute)
    monkeypatch.setattr(BillingRegistrationService, "_execute_kw", fake_execute_kw)
    monkeypatch.setattr(BillingRegistrationService, "_reconnect_with_tolerance", fake_reconnect_with_tolerance)

    processor = BillingAsyncProcessor()
    result = asyncio.run(
        processor.process_result(
            OCRJob(
                job_id="job-contact-center-payment-1",
                session_id="session-contact-center-payment-1",
                channel="whatsapp",
                recipient="593999",
                cedula="0907938674",
                contract={
                    "id": None,
                    "code": "200462",
                    "state": "cortado",
                    "residual": "22.89",
                    "source": "contact_center",
                    "partner": {"dni": "0907938674", "name": "FERNANDEZ TROYA MANUEL HIGINIO"},
                    "partner_invoice": {"id": None},
                    "franchise": {"id": None, "name": "MARVICNET CIA LTDA", "deposit": []},
                },
                attachments=[{"filename": "proof.png", "mime_type": "image/png", "base64_data": "aGVsbG8="}],
                message="Adjunto comprobante",
            ),
            {
                "estado": "validated",
                "debe_reintentar": False,
                "texto_extraido": "Comprobante 150989107 Banco Pichincha valor 20,00",
                "raw": {
                    "estado": "ok",
                    "banco": "Banco Pichincha",
                    "monto": "20,00",
                    "fecha": datetime.now(timezone.utc).date().isoformat(),
                    "numero_transaccion": "150989107",
                },
            },
            source="ocr_service_queue",
        )
    )

    assert result["status"] == "created"
    assert handoffs == []
    assert len(sent_messages) == 1
    assert "dejar el servicio reconectado" in sent_messages[0]["message"].lower()


def test_billing_async_processor_reports_reconnect_error_without_handoff(monkeypatch):
    sent_messages = []
    handoffs = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        sent_messages.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
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

    async def fake_register_payment(self, *, contract, ocr_result, attachments, cedula=None):
        return {
            "status": "created",
            "deposit_id": 856,
            "resolved": {
                "code": "225254",
                "value": 22.89,
                "pending_value": 22.89,
                "balance_due": 0.0,
                "reconnect_status": "error",
                "reconnect_error": "franchise_aes_key_not_configured",
            },
        }

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)
    monkeypatch.setattr(BillingRegistrationService, "register_payment", fake_register_payment)

    processor = BillingAsyncProcessor()
    result = asyncio.run(
        processor.process_result(
            OCRJob(
                job_id="job-contact-center-payment-error-1",
                session_id="session-contact-center-payment-error-1",
                channel="whatsapp",
                recipient="593999",
                cedula="0907938674",
                contract={
                    "id": None,
                    "code": "200462",
                    "state": "cortado",
                    "residual": "22.89",
                    "source": "contact_center",
                    "partner": {"dni": "0907938674", "name": "FERNANDEZ TROYA MANUEL HIGINIO"},
                    "partner_invoice": {"id": None},
                    "franchise": {"id": 1, "name": "MARVICNET CIA LTDA", "deposit": []},
                },
                attachments=[{"filename": "proof.png", "mime_type": "image/png", "base64_data": "aGVsbG8="}],
                message="Adjunto comprobante",
            ),
            {
                "estado": "validated",
                "debe_reintentar": False,
                "texto_extraido": "Comprobante 225254 Banco Pichincha valor 22,89",
                "raw": {
                    "estado": "ok",
                    "banco": "Banco Pichincha",
                    "monto": "22,89",
                    "fecha": datetime.now(timezone.utc).date().isoformat(),
                    "numero_transaccion": "225254",
                },
            },
            source="ocr_service_queue",
        )
    )

    assert result["status"] == "created"
    assert result["reconnect_status"] == "error"
    assert handoffs == []
    assert len(sent_messages) == 1
    assert "no pude completar la reconexión automática" in sent_messages[0]["message"].lower()


def test_billing_async_handoff_marks_session_human(monkeypatch):
    sent_messages = []
    handoffs = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        sent_messages.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
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
        return {"status": "sent", "channel_id": 7011}

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    store = build_session_store()
    asyncio.run(
        store.set(
            SessionState(
                session_id="billing-human-1",
                channel="whatsapp",
                recipient="593999",
                cedula="0102030405",
                current_intent="billing_proof_queued",
                last_agent="billing",
            )
        )
    )

    processor = BillingAsyncProcessor()
    result = asyncio.run(
        processor.process_result(
            OCRJob(
                job_id="job-human-1",
                session_id="billing-human-1",
                channel="whatsapp",
                recipient="593999",
                cedula="0102030405",
                contract={"code": "203493", "state": "cortado", "residual": "22.89"},
                attachments=[],
                message="Comprobante enviado",
            ),
            None,
            source="ocr_service_queue",
        )
    )

    assert result["status"] == "handoff"
    assert len(handoffs) == 1
    state = asyncio.run(store.get("billing-human-1"))
    assert state is not None
    assert state.human_handoff is True
    assert state.current_intent == "human_handoff"
    assert state.last_agent == "handoff"
    assert len(sent_messages) == 1




def test_ocr_callback_route_processes_billing_result(monkeypatch):
    sent_messages = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        sent_messages.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "actions": actions,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    async def fake_register(self, *, contract, ocr_result, attachments, cedula):
        return {
            "status": "created",
            "resolved": {"reconnect_status": "done"},
        }

    settings = get_settings()
    monkeypatch.setattr(settings, "ocr_callback_secret", "secret-123")
    monkeypatch.setattr(BillingRegistrationService, "register_payment", fake_register)
    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)

    client = TestClient(app)
    response = client.post(
        "/v1/ocr/callback",
        headers={"X-OCR-Callback-Secret": "secret-123"},
        json={
            "job": {
                "job_id": "job-callback-1",
                "session_id": "session-callback-1",
                "channel": "whatsapp",
                "recipient": "593999",
                "cedula": "0102030405",
                "contract": {"code": "CUT-0405", "residual": "18.50"},
                "attachments": [{"filename": "proof.png", "mime_type": "image/png", "base64_data": "abc123"}],
                "message": "Adjunto comprobante",
            },
            "ocr_result": {
                "status": "ok",
                "estado": "validated",
                "debe_reintentar": False,
                "texto_extraido": "Comprobante válido",
            },
            "source": "ocr_service_queue",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["result"]["status"] == "created"
    assert len(sent_messages) == 1
    assert sent_messages[0]["recipient"] == "593999"


def test_ocr_callback_route_rejects_invalid_secret(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ocr_callback_secret", "secret-123")

    client = TestClient(app)
    response = client.post(
        "/v1/ocr/callback",
        json={
            "job": {
                "job_id": "job-callback-2",
                "session_id": "session-callback-2",
                "channel": "whatsapp",
                "recipient": "593999",
                "contract": {},
                "attachments": [],
            },
            "ocr_result": {"status": "ok"},
        },
    )
    assert response.status_code == 401


def test_ocr_callback_route_requires_configured_secret(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ocr_callback_secret", None)

    client = TestClient(app)
    response = client.post(
        "/v1/ocr/callback",
        json={
            "job": {
                "job_id": "job-callback-missing-secret",
                "session_id": "session-callback-missing-secret",
                "channel": "whatsapp",
                "recipient": "593999",
                "contract": {},
                "attachments": [],
            },
            "ocr_result": {"status": "ok"},
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "ocr_callback_secret_not_configured"


def test_ocr_callback_route_is_idempotent_for_repeated_job_id(monkeypatch):
    sent_messages = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        sent_messages.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
            }
        )
        return {"status": "sent"}

    async def fake_register(self, *, contract, ocr_result, attachments, cedula):
        return {
            "status": "created",
            "resolved": {"reconnect_status": "done"},
        }

    settings = get_settings()
    monkeypatch.setattr(settings, "ocr_callback_secret", "secret-123")
    monkeypatch.setattr(BillingRegistrationService, "register_payment", fake_register)
    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)

    client = TestClient(app)
    payload = {
        "job": {
            "job_id": "job-callback-idempotent-1",
            "session_id": "session-callback-idempotent-1",
            "channel": "whatsapp",
            "recipient": "593999",
            "cedula": "0102030405",
            "contract": {"code": "CUT-0405", "residual": "18.50"},
            "attachments": [{"filename": "proof.png", "mime_type": "image/png", "base64_data": "abc123"}],
            "message": "Adjunto comprobante",
        },
        "ocr_result": {
            "status": "ok",
            "estado": "validated",
            "debe_reintentar": False,
            "texto_extraido": "Comprobante válido",
        },
        "source": "ocr_service_queue",
    }

    first = client.post("/v1/ocr/callback", headers={"X-OCR-Callback-Secret": "secret-123"}, json=payload)
    assert first.status_code == 200
    assert first.json()["result"]["status"] == "created"
    assert len(sent_messages) == 1

    second = client.post("/v1/ocr/callback", headers={"X-OCR-Callback-Secret": "secret-123"}, json=payload)
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["result"]["status"] == "created"
    assert len(sent_messages) == 1


def test_billing_flow_escalates_when_payment_registration_fails(monkeypatch):
    support_messages = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-009",
                    "state": "cortado",
                    "partner_name": "Juan Perez",
                    "residual": "18.00",
                    "id": 501,
                    "partner": {"dni": "0102030405", "name": "Juan Perez"},
                    "partner_invoice": {"id": 654},
                    "franchise": {
                        "id": 77,
                        "deposit": [{"id": 11, "name": "BANCO PICHINCHA", "number": "2100223078", "code": "PICH"}],
                    },
                }
            ],
        }

    async def fake_analyze(self, attachment, *, notify_gonet_bot: bool = False):
        return {
            "estado": "validated",
            "debe_reintentar": False,
            "texto_extraido": "Comprobante 59832714 Banco Pichincha valor 22,00",
            "raw": {"estado": "ok"},
        }

    async def fake_register(self, *, contract, ocr_result, attachments, cedula):
        return {
            "status": "error",
            "missing": [],
            "resolved": {
                "code": "59832714",
                "value": 22.00,
                "pending_value": 22.89,
                "balance_due": 0.89,
                "deposit": {"name": "BANCO PICHINCHA"},
            },
        }

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        support_messages.append(
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

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(OCRServiceClient, "enabled", property(lambda self: True))
    monkeypatch.setattr(OCRServiceClient, "analyze", fake_analyze)
    monkeypatch.setattr(BillingRegistrationService, "register_payment", fake_register)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "billing-register-error-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Necesito facturación",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert first.status_code == 200
    assert first.json()["intent"] == "consent_required"

    accepted = _accept_consent(client, session_id=session_id)
    assert accepted.status_code == 200

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Registrar Pago",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert second.status_code == 200

    third = client.post(
        "/v1/messages",
        json={
            "mensaje": "Adjunto comprobante",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
            "attachments": [
                {
                    "filename": "proof.png",
                    "mime_type": "image/png",
                    "base64_data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
                }
            ],
        },
    )
    assert third.status_code == 200
    body = third.json()
    assert body["agent"] == "handoff"
    assert "no pude registrar el pago automáticamente" in body["message"].lower()
    assert len(support_messages) == 1
    assert "Documento: 59832714" in support_messages[0]["summary"]
    assert "Valor pagado: 22.00" in support_messages[0]["summary"]


def test_billing_async_flow_escalates_when_queue_is_unavailable(monkeypatch):
    support_messages = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-ASYNC-ERR",
                    "state": "cortado",
                    "partner_name": "Juan Perez",
                    "residual": "25.00",
                    "id": 501,
                    "partner": {"dni": "0102030405", "name": "Juan Perez"},
                    "partner_invoice": {"id": 654},
                    "franchise": {
                        "id": 77,
                        "deposit": [{"id": 11, "name": "BANCO PICHINCHA", "number": "2100223078", "code": "PICH"}],
                    },
                }
            ],
        }

    async def fake_enqueue(self, job):
        raise OCRQueueUnavailableError("shared_ocr_queue_unavailable")

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        support_messages.append(
            {
                "channel": channel,
                "recipient": recipient,
                "summary": summary,
                "cedula": cedula,
            }
        )
        return {"status": "sent"}

    settings = get_settings()
    monkeypatch.setattr(settings, "ocr_async_enabled", True)
    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(OCRJobQueue, "enqueue", fake_enqueue)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "billing-async-queue-error-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Necesito facturación",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert first.status_code == 200
    assert first.json()["intent"] == "consent_required"

    accepted = _accept_consent(client, session_id=session_id)
    assert accepted.status_code == 200

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Registrar Pago",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert second.status_code == 200

    third = client.post(
        "/v1/messages",
        json={
            "mensaje": "Adjunto comprobante",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
            "attachments": [
                {
                    "filename": "proof.png",
                    "mime_type": "image/png",
                    "base64_data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
                }
            ],
        },
    )
    assert third.status_code == 200
    body = third.json()
    assert body["agent"] == "handoff"
    assert "no puedo enviarlo a validación automática" in body["message"].lower()
    assert len(support_messages) == 1
    assert "No fue posible enviar el comprobante" in support_messages[0]["summary"]
