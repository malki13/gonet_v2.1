import asyncio

from fastapi.testclient import TestClient

from tests.unit.bot_api_helpers import _accept_consent
from apps.bot_api.main import app
from packages.channels.delivery import ChannelDeliveryService
from packages.integrations.contract_lookup import ContractLookupClient
from packages.integrations.contact_registry import build_contact_registry
from packages.integrations.onu import ONUClient
from packages.integrations.odoo_chat import OdooChatClient
from packages.integrations.otp_service import OTPService
from packages.integrations.smarttelcom import SmartTelcomClient


def test_support_message_updates_contact_registry(monkeypatch):
    touched = []
    ai_marks = []
    registry = build_contact_registry()

    async def fake_touch_contact(**kwargs):
        touched.append(kwargs)
        return {"status": "updated"}

    async def fake_mark_ai_active(**kwargs):
        ai_marks.append(kwargs)
        return {"status": "updated"}

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-001",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    monkeypatch.setattr(registry, "touch_contact", fake_touch_contact)
    monkeypatch.setattr(registry, "mark_ai_active", fake_mark_ai_active)
    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: False)

    client = TestClient(app)
    response = client.post(
        "/v1/messages",
        json={
            "mensaje": "No tengo internet",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "registry-support-1",
            "cedula": "0104484043",
        },
    )
    assert response.status_code == 200
    assert len(touched) == 1
    assert touched[0]["recipient"] == "593999"
    assert touched[0]["red"] == "whatsapp"
    assert touched[0]["identificacion"] == "0104484043"
    assert len(ai_marks) == 1
    assert ai_marks[0]["recipient"] == "593999"
    assert ai_marks[0]["red"] == "whatsapp"
    assert ai_marks[0]["group"] == "support"


def test_send_endpoint_assigns_odoo_channel_in_contact_registry(monkeypatch):
    assignments = []
    human_marks = []
    registry = build_contact_registry()

    async def fake_assign_odoo_channel(**kwargs):
        assignments.append(kwargs)
        return {"status": "updated"}

    async def fake_mark_human_active(**kwargs):
        human_marks.append(kwargs)
        return {"status": "updated"}

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        return {"status": "sent"}

    monkeypatch.setattr(registry, "assign_odoo_channel", fake_assign_odoo_channel)
    monkeypatch.setattr(registry, "mark_human_active", fake_mark_human_active)
    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)

    client = TestClient(app)
    response = client.post(
        "/send",
        json={
            "chanel": "whatsapp",
            "recipient": "593999",
            "message": "Hola desde Odoo",
            "internal_user": 17,
            "channel_id": 88,
            "group": "support",
        },
    )
    assert response.status_code == 200
    assert assignments == [
        {
            "recipient": "593999",
            "red": "whatsapp",
            "internal_user": 17,
            "channel_id": 88,
        }
    ]
    assert human_marks == [
        {
            "recipient": "593999",
            "red": "whatsapp",
            "identificacion": None,
            "session_id": None,
            "group": "support",
            "chat_preview": "Hola desde Odoo",
        }
    ]


def test_support_flow_resumes_after_user_sends_cedula(monkeypatch):
    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-001",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        assert contrato == "CT-001"
        return {"ok": True, "data": {"accion": "monitoreo"}}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)
    client = TestClient(app)
    session_id = "support-cedula-1"

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
            "mensaje": "0104484043",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["agent"] == "clarify"
    assert second_body["intent"] == "consent_required"
    assert second_body["actions"]["type"] == "buttons"
    assert second_body["actions"]["buttons"][0]["id"] == "ASISTENCIA_ACEPTO"
    assert "aceptas el uso de la información" in second_body["message"].lower()

    third = _accept_consent(client, session_id=session_id)
    assert third.status_code == 200
    body = third.json()
    assert body["agent"] == "support"
    assert body["intent"] == "support_network_monitoring"
    assert "sigue igual" in body["message"].lower()


def test_support_flow_keeps_asking_for_identity_on_invalid_document_candidate(monkeypatch):
    calls = []

    async def fake_info(self, cedula: str):
        calls.append(cedula)
        return {"ok": True, "data": []}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    client = TestClient(app)
    session_id = "support-invalid-doc-1"

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
            "mensaje": "mi número es 12345678",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    assert second.json()["intent"] == "ask_cedula"
    assert calls == []


def test_support_flow_gives_short_guidance_when_user_does_not_remember_cedula(monkeypatch):
    handoffs = []

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

    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)
    client = TestClient(app)
    session_id = "support-no-cedula-guidance-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Tengo el internet lento",
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
            "mensaje": "No recuerdo mi cédula",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["intent"] == "human_handoff"
    assert "dame un momento" in second_body["message"].lower()
    assert "asesor especializado" in second_body["message"].lower()
    assert len(handoffs) == 1

    third = client.post(
        "/v1/messages",
        json={
            "mensaje": "Todavía no tengo la cédula",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert third.status_code == 200
    third_body = third.json()
    assert third_body["intent"] == "human_handoff"
    assert "dame un momento" in third_body["message"].lower()


def test_contact_flow_asks_for_contract_holder_cedula_when_lookup_returns_no_contracts(monkeypatch):
    calls = []

    async def fake_info(self, cedula: str):
        calls.append(cedula)
        if cedula == "0104484043":
            return {"ok": True, "data": []}
        if cedula == "0703042374001":
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
        return {"ok": True, "data": []}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    client = TestClient(app)
    session_id = "billing-contract-holder-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Necesito facturación",
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
            "mensaje": "0104484043",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["intent"] == "ask_cedula"
    assert "titular del contrato" in second_body["message"].lower()
    assert "no encontré contratos" in second_body["message"].lower()

    third = client.post(
        "/v1/messages",
        json={
            "mensaje": "0703042374001",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert third.status_code == 200
    third_body = third.json()
    assert third_body["agent"] == "clarify"
    assert third_body["intent"] == "consent_required"

    fourth = _accept_consent(client, session_id=session_id)
    assert fourth.status_code == 200
    fourth_body = fourth.json()
    assert fourth_body["agent"] == "billing"
    assert fourth_body["intent"] == "billing"
    assert "ct-002" in fourth_body["message"].lower()
    assert calls == ["0104484043", "0703042374001"]


def test_contact_flow_escalates_after_two_cedula_attempts_without_contracts(monkeypatch):
    calls = []
    handoffs = []

    async def fake_info(self, cedula: str):
        calls.append(cedula)
        return {"ok": True, "data": []}

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

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)
    client = TestClient(app)
    session_id = "billing-contract-holder-loop-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Necesito facturación",
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
            "mensaje": "0104484043",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    assert second.json()["intent"] == "ask_cedula"

    third = client.post(
        "/v1/messages",
        json={
            "mensaje": "0707017315",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert third.status_code == 200
    third_body = third.json()
    assert third_body["agent"] == "handoff"
    assert third_body["intent"] == "human_handoff"
    assert "dame un momento" in third_body["message"].lower()
    assert "asesor especializado" in third_body["message"].lower()
    assert len(handoffs) == 1
    assert "después de 2 intentos" in (handoffs[0]["summary"] or "").lower()
    assert handoffs[0]["cedula"] == "0707017315"
    assert calls == ["0104484043", "0707017315"]


def test_support_flow_runs_monitoring_conversationally_and_closes_when_resolved(monkeypatch):
    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-001",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        assert contrato == "CT-001"
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 2}}}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)

    client = TestClient(app)
    session_id = "support-monitor-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "No tengo internet",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["intent"] == "consent_required"
    assert first_body["actions"]["buttons"][0]["id"] == "ASISTENCIA_ACEPTO"

    second = _accept_consent(client, session_id=session_id)
    assert second.status_code == 200
    body = second.json()
    assert body["agent"] == "support"
    assert body["intent"] == "support_network_monitoring"
    assert "sigue igual" in body["message"].lower()

    third = client.post(
        "/v1/messages",
        json={
            "mensaje": "Sí, ya funciona",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert third.status_code == 200
    assert third.json()["intent"] == "support_network_resolved"


def test_support_followup_reinterprets_new_symptom_and_reruns_monitoring(monkeypatch):
    calls = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-001",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        calls.append(contrato)
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 2}}}

    async def fake_connected(self, contrato: str):
        return {"ok": True, "data": {"count": 3, "devices": [{}, {}, {}]}}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)
    monkeypatch.setattr(SmartTelcomClient, "connected_devices_for_contract", fake_connected)
    monkeypatch.setattr(ONUClient, "enabled", lambda self: False)

    client = TestClient(app)
    session_id = "support-followup-reclassify-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "No tengo internet",
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
    assert accepted.json()["intent"] == "support_network_monitoring"

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Estoy con el internet lento",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body["intent"] == "support_network_monitoring"
    assert "lentitud" in body["message"].lower()
    assert calls == ["CT-001", "CT-001"]


def test_support_followup_repeated_same_symptom_escalates(monkeypatch):
    handoffs = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-001",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 2}}}

    async def fake_connected(self, contrato: str):
        return {"ok": True, "data": {"count": 1, "devices": [{}]}}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(summary or "")
        return {"status": "sent"}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)
    monkeypatch.setattr(SmartTelcomClient, "connected_devices_for_contract", fake_connected)
    monkeypatch.setattr(ONUClient, "enabled", lambda self: False)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "support-followup-repeat-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Está lento",
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
    assert accepted.json()["intent"] == "support_network_monitoring"

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Está lento",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body["agent"] == "handoff"
    assert "asesor especializado" in body["message"].lower()
    assert len(handoffs) == 1


def test_support_followup_escalates_when_user_says_todavia_sigue(monkeypatch):
    handoffs = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-001",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 2}}}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(
            {
                "channel": channel,
                "recipient": recipient,
                "summary": summary,
                "cedula": cedula,
            }
        )
        return {"status": "sent"}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "support-followup-negative-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "No tengo internet",
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
    assert accepted.json()["intent"] == "support_network_monitoring"

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Todavía sigue",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body["agent"] == "handoff"
    assert "asesor especializado" in body["message"].lower()
    assert len(handoffs) == 1
    assert "persiste" in handoffs[0]["summary"].lower()


def test_support_followup_escalates_when_user_says_sigue_lento(monkeypatch):
    handoffs = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-001",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 2}}}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(summary or "")
        return {"status": "sent"}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)
    monkeypatch.setattr(ONUClient, "enabled", lambda self: False)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "support-followup-sigue-lento-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "No tengo internet",
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
    assert accepted.json()["intent"] == "support_network_monitoring"

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Sigue lento",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert body["agent"] == "handoff"
    assert "asesor especializado" in body["message"].lower()
    assert len(handoffs) == 1


def test_support_monitoring_requests_manual_checks_when_onu_status_query_fails(monkeypatch):
    handoffs = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-ONU-FAIL",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 2}}}

    async def fake_onu_status(self, contrato: str):
        raise RuntimeError("onu backend failed")

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(summary or "")
        return {"status": "sent"}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)
    monkeypatch.setattr(ONUClient, "enabled", lambda self: True)
    monkeypatch.setattr(ONUClient, "get_status", fake_onu_status)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "support-onu-fail-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Está lento",
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
    assert body["agent"] == "support"
    assert body["intent"] == "support_manual_checks"
    assert "reinicia manualmente la onu y el router" in body["message"].lower()
    assert "red correcta" in body["message"].lower()
    assert "sigue igual" in body["message"].lower()
    assert len(handoffs) == 0


def test_support_monitoring_failure_for_no_service_requests_manual_checks(monkeypatch):
    handoffs = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-NO-SERVICE",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        return {"ok": False, "error": "smart backend failed"}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(summary or "")
        return {"status": "sent"}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "support-no-service-guided-handoff-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "No tengo internet",
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
    assert body["agent"] == "support"
    assert body["intent"] == "support_manual_checks"
    assert "onu y el router" in body["message"].lower()
    assert "otro tomacorriente" in body["message"].lower()
    assert "sigue igual" in body["message"].lower()
    assert len(handoffs) == 0


def test_support_monitoring_reboot_failure_requests_manual_checks_before_handoff(monkeypatch):
    handoffs = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-SLOW-REBOOT-FAIL",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 3}}}

    async def fake_onu_status(self, contrato: str):
        return {"ok": True, "status": "working", "power_dbm": -25.9}

    async def fake_onu_reboot(self, contrato: str):
        raise RuntimeError("onu reboot failed")

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(summary or "")
        return {"status": "sent"}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)
    monkeypatch.setattr(ONUClient, "enabled", lambda self: True)
    monkeypatch.setattr(ONUClient, "get_status", fake_onu_status)
    monkeypatch.setattr(ONUClient, "reboot", fake_onu_reboot)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "support-slow-reboot-fallback-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Tengo el internet lento",
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
    assert body["agent"] == "support"
    assert body["intent"] == "support_manual_checks"
    assert "reinicia manualmente la onu y el router" in body["message"].lower()
    assert "sigue igual" in body["message"].lower()
    assert "asesor especializado" in body["message"].lower()
    assert len(handoffs) == 0


def test_support_monitoring_reboots_onu_and_router_before_escalating(monkeypatch):
    handoffs = []
    onu_calls = {"status": 0, "reboot": 0}
    router_reboots = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-ONU-1",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 4}}}

    async def fake_onu_status(self, contrato: str):
        onu_calls["status"] += 1
        return {"ok": True, "status": "los", "power_dbm": -26.8}

    async def fake_onu_reboot(self, contrato: str):
        onu_calls["reboot"] += 1
        return {"ok": True, "status": "accepted"}

    async def fake_router_reboot(self, contrato: str):
        router_reboots.append(contrato)
        return {"ok": True, "data": {"status": "accepted"}}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(summary or "")
        return {"status": "sent"}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)
    monkeypatch.setattr(SmartTelcomClient, "reboot_router_for_contract", fake_router_reboot)
    monkeypatch.setattr(ONUClient, "enabled", lambda self: True)
    monkeypatch.setattr(ONUClient, "get_status", fake_onu_status)
    monkeypatch.setattr(ONUClient, "reboot", fake_onu_reboot)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "support-onu-reboot-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "No tengo internet",
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
    assert body["agent"] == "handoff"
    assert "asesor especializado" in body["message"].lower()
    assert onu_calls["status"] == 2
    assert onu_calls["reboot"] == 1
    assert router_reboots == ["CT-ONU-1"]
    assert len(handoffs) == 1
    assert "alerta onu" in handoffs[0].lower() or "estado los" in handoffs[0].lower()


def test_support_monitoring_reports_connected_devices_for_slow_internet(monkeypatch):
    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-SLOW-1",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 2}}}

    async def fake_onu_status(self, contrato: str):
        return {"ok": True, "status": "working", "power_dbm": -26.4}

    async def fake_connected(self, contrato: str):
        return {"ok": True, "data": {"count": 5, "devices": [{}, {}, {}, {}, {}]}}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)
    monkeypatch.setattr(SmartTelcomClient, "connected_devices_for_contract", fake_connected)
    monkeypatch.setattr(ONUClient, "enabled", lambda self: True)
    monkeypatch.setattr(ONUClient, "get_status", fake_onu_status)

    client = TestClient(app)
    session_id = "support-slow-devices-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Tengo el internet lento",
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
    assert body["intent"] == "support_network_monitoring"
    assert "5" in body["message"]
    assert "dispositivos" in body["message"].lower()


def test_support_monitoring_reboots_onu_and_router_for_intermittence(monkeypatch):
    onu_reboots = []
    router_reboots = []
    onu_status_calls = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-INT-1",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 4}}}

    async def fake_onu_status(self, contrato: str):
        onu_status_calls.append(contrato)
        return {"ok": True, "status": "working", "power_dbm": -26.2}

    async def fake_onu_reboot(self, contrato: str):
        onu_reboots.append(contrato)
        return {"ok": True, "status": "accepted"}

    async def fake_router_reboot(self, contrato: str):
        router_reboots.append(contrato)
        return {"ok": True, "data": {"status": "accepted"}}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)
    monkeypatch.setattr(SmartTelcomClient, "reboot_router_for_contract", fake_router_reboot)
    monkeypatch.setattr(ONUClient, "enabled", lambda self: True)
    monkeypatch.setattr(ONUClient, "get_status", fake_onu_status)
    monkeypatch.setattr(ONUClient, "reboot", fake_onu_reboot)

    client = TestClient(app)
    session_id = "support-intermittence-reboot-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Se cae el internet a ratos",
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
    assert body["intent"] == "support_network_monitoring"
    assert "reinicio remoto" in body["message"].lower()
    assert onu_status_calls == ["CT-INT-1", "CT-INT-1"]
    assert onu_reboots == ["CT-INT-1"]
    assert router_reboots == ["CT-INT-1"]


def test_support_generic_network_requests_issue_triage_before_running_monitoring(monkeypatch):
    handoffs = []
    onu_reboots = []
    router_reboots = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-GEN-1",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 4}}}

    async def fake_onu_status(self, contrato: str):
        raise RuntimeError("onu backend failed")

    async def fake_onu_reboot(self, contrato: str):
        onu_reboots.append(contrato)
        return {"ok": True, "status": "accepted"}

    async def fake_router_reboot(self, contrato: str):
        router_reboots.append(contrato)
        return {"ok": True, "data": {"status": "accepted"}}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(summary or "")
        return {"status": "sent"}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)
    monkeypatch.setattr(SmartTelcomClient, "reboot_router_for_contract", fake_router_reboot)
    monkeypatch.setattr(ONUClient, "enabled", lambda self: True)
    monkeypatch.setattr(ONUClient, "get_status", fake_onu_status)
    monkeypatch.setattr(ONUClient, "reboot", fake_onu_reboot)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "support-generic-reboot-failover-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Tengo problemas con internet",
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
    assert body["agent"] == "support"
    assert body["intent"] == "support_clarify"
    assert "sin servicio" in body["message"].lower()
    assert "se va por momentos" in body["message"].lower()
    assert "esta lento" in body["message"].lower() or "está lento" in body["message"].lower()
    assert "reinicia manualmente la onu y el router" in body["message"].lower()
    assert onu_reboots == []
    assert router_reboots == []
    assert len(handoffs) == 0

    detailed = client.post(
        "/v1/messages",
        json={
            "mensaje": "Ya hice eso, se cae el internet a ratos",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert detailed.status_code == 200
    detail_body = detailed.json()
    assert detail_body["agent"] == "handoff"
    assert detail_body["intent"] == "human_handoff"
    assert "gracias por realizar esas validaciones" in detail_body["message"].lower()
    assert "asesor especializado" in detail_body["message"].lower()
    assert onu_reboots == ["CT-GEN-1"]
    assert router_reboots == ["CT-GEN-1"]
    assert len(handoffs) == 1


def test_support_slow_internet_after_manual_checks_escalates_without_repeating_restart(monkeypatch):
    handoffs = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-SLOW-AFTER-CHECKS",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_monitor(self, contrato: str):
        return {"ok": True, "data": {"accion": "monitoreo", "info": {"numeroRedes": 4}}}

    async def fake_onu_status(self, contrato: str):
        raise RuntimeError("onu backend failed")

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(summary or "")
        return {"status": "sent"}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "monitor_contract", fake_monitor)
    monkeypatch.setattr(ONUClient, "enabled", lambda self: True)
    monkeypatch.setattr(ONUClient, "get_status", fake_onu_status)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "support-slow-after-checks-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Tengo problemas con internet",
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
    assert accepted.json()["intent"] == "support_clarify"

    detailed = client.post(
        "/v1/messages",
        json={
            "mensaje": "Ya hice eso, mi internet está lento",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert detailed.status_code == 200
    body = detailed.json()
    assert body["agent"] == "handoff"
    assert body["intent"] == "human_handoff"
    assert "gracias por realizar esas validaciones" in body["message"].lower()
    assert "reinicia manualmente" not in body["message"].lower()
    assert len(handoffs) == 1


def test_support_flow_escalates_temp_network_requests_instead_of_exposing_them(monkeypatch):
    handoffs = []

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-TEMP-BLOCK",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(summary or "")
        return {"status": "sent"}

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)

    client = TestClient(app)
    session_id = "support-temp-block-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "activar red 2",
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
    assert body["agent"] == "handoff"
    assert "asesor especializado" in body["message"].lower()
    assert len(handoffs) == 1


def test_support_edit_network_flow_runs_through_otp_and_change(monkeypatch):
    captured_change = {}

    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-777",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_generate(self, recipient: str, session_id: str, cedula: str | None):
        return {"ok": True, "data": {"sent_to": "juan@test.com"}}

    async def fake_verify(self, recipient: str, session_id: str, otp: str):
        return {"ok": True, "data": {"verified": True, "attempts_left": 3}}

    async def fake_list_networks(self, contrato: str):
        raise AssertionError("list_networks_for_contract should not be called after OTP verification")

    async def fake_change_networks(self, contrato: str, nuevo_nombre: str, password: str):
        captured_change["contrato"] = contrato
        captured_change["nuevo_nombre"] = nuevo_nombre
        captured_change["password"] = password
        return {
            "ok": True,
            "data": {
                "mensaje": (
                    "Listo, ya quedó actualizado. Tus redes wifi ahora se llaman "
                    "MiWifi 2.4G y MiWifi 5G. Usa la nueva clave para volver a conectarte."
                )
            },
        }

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(OTPService, "generate_otp", fake_generate)
    monkeypatch.setattr(OTPService, "verify_otp", fake_verify)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "list_networks_for_contract", fake_list_networks)
    monkeypatch.setattr(SmartTelcomClient, "change_networks_for_contract", fake_change_networks)

    client = TestClient(app)
    session_id = "support-edit-1"

    one = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero cambiar la contraseña del wifi",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert one.status_code == 200
    assert one.json()["intent"] == "consent_required"

    accepted = _accept_consent(client, session_id=session_id)
    assert accepted.status_code == 200
    assert accepted.json()["intent"] == "support_edit_network_otp_sent"

    two = client.post(
        "/v1/messages",
        json={
            "mensaje": "A1B2C3",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert two.status_code == 200
    two_body = two.json()
    assert two_body["intent"] == "support_edit_network_ready"
    assert "ya validé el código" in two_body["message"].lower()
    assert "nuevo nombre de tu wifi" in two_body["message"].lower()
    assert "cómo se llaman hoy tus redes actuales" in two_body["message"].lower()
    assert "miwifi 2.4g" in two_body["message"].lower()
    assert "miwifi 5g" in two_body["message"].lower()
    assert "red id" not in two_body["message"].lower()

    three = client.post(
        "/v1/messages",
        json={
            "mensaje": "nombre: MiWifi, contraseña: MiClave123",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert three.status_code == 200
    three_body = three.json()
    assert three_body["intent"] == "support_edit_network_done"
    assert "ya quedó actualizado" in three_body["message"].lower()
    assert "miwifi 2.4g" in three_body["message"].lower()
    assert "miwifi 5g" in three_body["message"].lower()
    assert "nueva clave" in three_body["message"].lower()
    assert captured_change == {
        "contrato": "CT-777",
        "nuevo_nombre": "MiWifi",
        "password": "MiClave123",
    }


def test_support_edit_network_flow_can_show_current_primary_network_names_after_otp(monkeypatch):
    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-778",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_generate(self, recipient: str, session_id: str, cedula: str | None):
        return {"ok": True, "data": {"sent_to": "juan@test.com"}}

    async def fake_verify(self, recipient: str, session_id: str, otp: str):
        return {"ok": True, "data": {"verified": True, "attempts_left": 3}}

    async def fake_list_networks(self, contrato: str):
        return {
            "ok": True,
            "data": {
                "numero_redes": 2,
                "networks": [
                    {"Red": 1, "NombreRed": "Casa Perez", "EstadoRed": True},
                    {"Red": 2, "NombreRed": "Casa Perez_5G", "EstadoRed": True},
                ],
            },
        }

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(OTPService, "generate_otp", fake_generate)
    monkeypatch.setattr(OTPService, "verify_otp", fake_verify)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "list_networks_for_contract", fake_list_networks)

    client = TestClient(app)
    session_id = "support-edit-show-names-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero cambiar la clave del wifi",
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
    assert accepted.json()["intent"] == "support_edit_network_otp_sent"

    otp = client.post(
        "/v1/messages",
        json={
            "mensaje": "A1B2C3",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert otp.status_code == 200
    assert otp.json()["intent"] == "support_edit_network_ready"

    show = client.post(
        "/v1/messages",
        json={
            "mensaje": "como se llaman ?",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert show.status_code == 200
    show_body = show.json()
    assert show_body["intent"] == "support_edit_network_credentials"
    assert "así se llaman actualmente tus redes principales" in show_body["message"].lower()
    assert "red 2.4g: casa perez" in show_body["message"].lower()
    assert "red 5g: casa perez_5g" in show_body["message"].lower()
    assert "nombre: miwifi" in show_body["message"].lower()


def test_support_edit_network_flow_keeps_going_when_current_network_lookup_fails(monkeypatch):
    async def fake_info(self, cedula: str):
        return {
            "ok": True,
            "data": [
                {
                    "code": "CT-779",
                    "state": "active",
                    "partner_name": "Juan Perez",
                }
            ],
        }

    async def fake_generate(self, recipient: str, session_id: str, cedula: str | None):
        return {"ok": True, "data": {"sent_to": "juan@test.com"}}

    async def fake_verify(self, recipient: str, session_id: str, otp: str):
        return {"ok": True, "data": {"verified": True, "attempts_left": 3}}

    async def fake_list_networks(self, contrato: str):
        raise RuntimeError("smart timeout")

    monkeypatch.setattr(ContractLookupClient, "info_personal_by_cedula", fake_info)
    monkeypatch.setattr(OTPService, "generate_otp", fake_generate)
    monkeypatch.setattr(OTPService, "verify_otp", fake_verify)
    monkeypatch.setattr(SmartTelcomClient, "enabled", lambda self: True)
    monkeypatch.setattr(SmartTelcomClient, "list_networks_for_contract", fake_list_networks)

    client = TestClient(app)
    session_id = "support-edit-show-names-fail-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero cambiar la clave del wifi",
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
    assert accepted.json()["intent"] == "support_edit_network_otp_sent"

    otp = client.post(
        "/v1/messages",
        json={
            "mensaje": "A1B2C3",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert otp.status_code == 200
    assert otp.json()["intent"] == "support_edit_network_ready"

    show = client.post(
        "/v1/messages",
        json={
            "mensaje": "Muéstrame mis redes actuales",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
            "cedula": "0102030405",
        },
    )
    assert show.status_code == 200
    show_body = show.json()
    assert show_body["intent"] == "support_edit_network_credentials"
    assert "no pude consultar en este momento" in show_body["message"].lower()
    assert "igual podemos continuar" in show_body["message"].lower()
    assert "nombre: miwifi" in show_body["message"].lower()


def test_smarttelcom_change_networks_uses_base_name_for_both_bands(monkeypatch):
    calls = []

    async def fake_get_by_contrato(self, contrato: str):
        return {"data": {"dispositivoId": "device-1", "numeroRedes": 2}}

    async def fake_get_all_networks_device(self, dispositivo_id: str):
        return {"data": [{"Red": 1}, {"Red": 2}]}

    async def fake_change_red(self, dispositivo_id: str, red_id: str, nombre_red: str, contrasena_red: str, estado_red: bool):
        calls.append(
            {
                "dispositivo_id": dispositivo_id,
                "red_id": red_id,
                "nombre_red": nombre_red,
                "contrasena_red": contrasena_red,
                "estado_red": estado_red,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(SmartTelcomClient, "get_by_contrato", fake_get_by_contrato)
    monkeypatch.setattr(SmartTelcomClient, "get_all_networks_device", fake_get_all_networks_device)
    monkeypatch.setattr(SmartTelcomClient, "change_red", fake_change_red)

    client = SmartTelcomClient()
    out = asyncio.run(client.change_networks_for_contract("CT-888", "Casa 5G", "Clave12345"))

    assert out["ok"] is True
    assert out["data"]["mensaje"] == (
        "Listo, ya quedó actualizado. Tus redes wifi ahora se llaman Casa 2.4G y Casa 5G. "
        "Usa la nueva clave para volver a conectarte."
    )
    assert calls == [
        {
            "dispositivo_id": "device-1",
            "red_id": "1",
            "nombre_red": "Casa 2.4G",
            "contrasena_red": "Clave12345",
            "estado_red": True,
        },
        {
            "dispositivo_id": "device-1",
            "red_id": "2",
            "nombre_red": "Casa 5G",
            "contrasena_red": "Clave12345",
            "estado_red": True,
        },
    ]
