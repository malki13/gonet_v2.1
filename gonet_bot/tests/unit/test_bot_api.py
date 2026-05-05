import asyncio
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apps.bot_api.main import app
from apps.bot_api.routes import gateway as gateway_routes
from packages.agents.contact_utils import classify_support_issue
from packages.agents.billing_async import BillingAsyncProcessor
from packages.channels.delivery import ChannelDeliveryService
from packages.channels import media_proxy
from packages.integrations.billing_registration import BillingRegistrationService
from packages.integrations.contact_registry import build_contact_registry
from packages.integrations.ocr_service_client import OCRServiceClient
from packages.integrations.odoo_chat import OdooChatClient
from packages.integrations.ocr_queue import OCRJobQueue
from packages.integrations.redis_store import build_session_store
from packages.orchestrator.inactivity import InactivityService
from packages.orchestrator.router import IntentRouter
from packages.orchestrator.service import OrchestratorService
from packages.orchestrator.session_context import SessionContextService
from packages.shared.config import get_settings
from packages.shared.errors import OCRQueueUnavailableError
from packages.shared.schemas import Attachment, InboundMessage, OCRJob, OutboundMessage, SessionState


def test_health_endpoint():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_media_proxy_keeps_public_base_when_internal_request_arrives(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    media_proxy._runtime_base_url = None
    media_proxy.register_runtime_base_url("https://demo.ngrok-free.app")
    media_proxy.register_runtime_base_url("http://host.docker.internal:8010")
    assert media_proxy.build_public_media_url("archivo.png") == "https://demo.ngrok-free.app/media/archivo.png"
    media_proxy._runtime_base_url = None


def test_root_verification_endpoint(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "verify_token", "verify-123")

    client = TestClient(app)
    response = client.get(
        "/",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-123",
            "hub.challenge": "abc123",
        },
    )
    assert response.status_code == 200
    assert response.text == "abc123"


def test_odoo_chat_client_prefers_direct_public_chat(monkeypatch):
    captured = {}

    async def fake_post_json(self, target_url: str, payload: dict):
        captured["target_url"] = target_url
        captured["payload"] = payload
        return {"status": "sent", "target_url": target_url, "payload": payload}

    settings = get_settings()
    monkeypatch.setattr(settings, "url_odoo_chat", "http://host.docker.internal:8079/api/public_chat")
    monkeypatch.setattr(settings, "odoo_public_token", "token-demo")
    monkeypatch.setattr(settings, "odoo_client_email", "chat@gonet.ec")
    monkeypatch.setattr(OdooChatClient, "_post_json", fake_post_json)

    client = OdooChatClient()
    result = asyncio.run(
        client.escalate_new_client(
            channel="whatsapp",
            recipient="593999",
            summary="Cliente solicita derivación con asesor especializado.",
            cedula="0102030405",
            origen="gonet",
        )
    )
    assert result["status"] == "sent"
    assert captured["target_url"] == "http://host.docker.internal:8079/api/public_chat"
    assert captured["payload"]["token"] == "token-demo"
    assert captured["payload"]["client_email"] == "chat@gonet.ec"
    assert captured["payload"]["vat"] == "0102030405"
    assert captured["payload"]["recipient"] == "593999"
    assert captured["payload"]["chanel"] == "whatsapp"
    assert captured["payload"]["message"] == "Cliente solicita derivación con asesor especializado."


def test_root_whatsapp_webhook_processes_and_delivers(monkeypatch):
    deliveries = []

    async def fake_handle_message(self, payload):
        return OutboundMessage(
            status="ok",
            message="Respuesta demo",
            agent="sales",
            intent="commercial",
            confidence=0.95,
        )

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        deliveries.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "actions": actions,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    monkeypatch.setattr(OrchestratorService, "handle_message", fake_handle_message)
    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)

    client = TestClient(app)
    response = client.post(
        "/",
        json={
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.demo.1",
                                        "from": "593999",
                                        "type": "text",
                                        "text": {"body": "Hola"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.text == "EVENT_RECEIVED"
    assert len(deliveries) == 1
    assert deliveries[0]["channel"] == "whatsapp"
    assert deliveries[0]["recipient"] == "593999"
    assert deliveries[0]["message"] == "Respuesta demo"


def test_root_whatsapp_webhook_requires_valid_meta_signature_when_configured(monkeypatch):
    deliveries = []

    async def fake_handle_message(self, payload):
        return OutboundMessage(
            status="ok",
            message="Respuesta segura",
            agent="sales",
            intent="commercial",
            confidence=0.95,
        )

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        deliveries.append({"channel": channel, "recipient": recipient, "message": message})
        return {"status": "sent"}

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_secret", "meta-secret-123")
    monkeypatch.setattr(OrchestratorService, "handle_message", fake_handle_message)
    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.demo.signed",
                                    "from": "593999",
                                    "type": "text",
                                    "text": {"body": "Hola"},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    raw_body = json.dumps(payload).encode("utf-8")
    valid_signature = "sha256=" + hmac.new(b"meta-secret-123", raw_body, hashlib.sha256).hexdigest()

    client = TestClient(app)

    rejected = client.post(
        "/",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=bad"},
    )
    assert rejected.status_code == 401
    assert rejected.json()["detail"] == "invalid_meta_signature"
    assert deliveries == []

    accepted = client.post(
        "/",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": valid_signature},
    )
    assert accepted.status_code == 200
    assert accepted.text == "EVENT_RECEIVED"
    assert len(deliveries) == 1
    assert deliveries[0]["message"] == "Respuesta segura"


def test_root_internal_dispatch_requires_internal_secret_when_configured(monkeypatch):
    dispatched = []

    async def fake_dispatch(payload: dict):
        dispatched.append(payload)

    settings = get_settings()
    monkeypatch.setattr(settings, "bot_api_internal_secret", "internal-secret-123")
    monkeypatch.setattr(gateway_routes, "_dispatch_internal_send", fake_dispatch)

    client = TestClient(app)
    payload = {"channel": "whatsapp", "recipient": "593999", "message": "hola"}

    rejected = client.post("/", json=payload)
    assert rejected.status_code == 401
    assert rejected.json()["detail"] == "invalid_internal_secret"
    assert dispatched == []

    accepted = client.post("/", json=payload, headers={"Authorization": "Bearer internal-secret-123"})
    assert accepted.status_code == 200
    assert accepted.text == "Enviado"
    assert len(dispatched) == 1


def test_root_whatsapp_webhook_generates_new_session_when_registry_row_is_missing(monkeypatch):
    captured = {}

    class FakeRegistry:
        async def resolve_session(self, **kwargs):
            return {"status": "missing", "exists": False, "session_id": None}

    async def fake_handle_message(self, payload):
        captured["session_id"] = payload.session_id
        return OutboundMessage(
            status="ok",
            message="Respuesta nueva",
            agent="clarify",
            intent="clarify",
            confidence=0.95,
        )

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        captured["recipient"] = recipient
        return {"status": "sent"}

    stale_store = build_session_store()
    asyncio.run(
        stale_store.set(
            SessionState(
                session_id="593999",
                channel="whatsapp",
                recipient="593999",
                current_intent="billing",
                cedula="0703042374001",
            )
        )
    )

    monkeypatch.setattr(gateway_routes, "build_contact_registry", lambda: FakeRegistry())
    monkeypatch.setattr(OrchestratorService, "handle_message", fake_handle_message)
    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)

    client = TestClient(app)
    response = client.post(
        "/",
        json={
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.demo.2",
                                        "from": "593999",
                                        "type": "text",
                                        "text": {"body": "Hola"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.text == "EVENT_RECEIVED"
    assert captured["recipient"] == "593999"
    assert captured["session_id"] != "593999"
    assert asyncio.run(stale_store.get("593999")) is None


def test_root_whatsapp_status_webhook_is_acknowledged_without_processing(monkeypatch):
    deliveries = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        deliveries.append({"channel": channel, "recipient": recipient, "message": message})
        return {"status": "sent"}

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)

    client = TestClient(app)
    response = client.post(
        "/",
        json={
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": "wamid.status.1",
                                        "status": "delivered",
                                        "recipient_id": "593999",
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.text == "EVENT_RECEIVED"
    assert deliveries == []


def test_root_webhook_acknowledges_invalid_payload_without_retry():
    client = TestClient(app)
    response = client.post("/", content="payload-invalido", headers={"Content-Type": "text/plain"})
    assert response.status_code == 200
    assert response.text == "EVENT_RECEIVED"


def test_classify_support_issue_detects_more_specific_support_signals():
    assert classify_support_issue("El internet va y viene") == "intermittence"
    assert classify_support_issue("Por momentos se corta el internet") == "intermittence"
    assert classify_support_issue("El internet esta muy lento") == "slow_internet"
    assert classify_support_issue("Se pone lento cerca de la noche") == "slow_internet"


def test_orchestrator_fallbacks_to_handoff_on_router_error(monkeypatch):
    handoffs = []

    async def fake_decide(self, message, state):
        raise RuntimeError("llm unavailable")

    async def fake_escalate_new_client(self, *, channel="internal", recipient="", summary=None, cedula=None, origen=None, group=None):
        handoffs.append(
            {
                "summary": summary,
                "channel": channel,
                "recipient": recipient,
                "cedula": cedula,
                "origen": origen,
            }
        )
        return {"status": "sent"}

    monkeypatch.setattr(IntentRouter, "decide", fake_decide)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_escalate_new_client)

    service = OrchestratorService()
    outbound = asyncio.run(
        service.handle_message(
            InboundMessage(
                mensaje="Hola",
                channel="whatsapp",
                recipient="593999",
                session_id="orchestrator-router-error-1",
            )
        )
    )

    assert outbound.agent == "handoff"
    assert outbound.intent == "human_handoff"
    assert "asesor especializado" in outbound.message.lower()
    assert len(handoffs) == 1
    assert handoffs[0]["channel"] == "whatsapp"
    assert "RuntimeError" in handoffs[0]["summary"]
    assert "llm unavailable" in handoffs[0]["summary"]




def test_root_whatsapp_webhook_escalates_when_delivery_fails(monkeypatch):
    handoffs = []

    async def fake_handle_message(self, payload):
        return OutboundMessage(
            status="ok",
            message="Respuesta demo",
            agent="support",
            intent="support",
            confidence=0.95,
        )

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        raise RuntimeError("graph delivery failed")

    async def fake_escalate_new_client(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(
            {
                "channel": channel,
                "recipient": recipient,
                "summary": summary or "",
                "cedula": cedula,
            }
        )
        return {"status": "sent"}

    monkeypatch.setattr(OrchestratorService, "handle_message", fake_handle_message)
    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_escalate_new_client)

    client = TestClient(app)
    response = client.post(
        "/",
        json={
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "wamid.demo.delivery.fail",
                                        "from": "593999",
                                        "type": "text",
                                        "text": {"body": "Hola"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.text == "EVENT_RECEIVED"
    assert len(handoffs) == 1
    assert handoffs[0]["channel"] == "whatsapp"
    assert handoffs[0]["recipient"] == "593999"
    assert "RuntimeError" in handoffs[0]["summary"]
    assert "graph delivery failed" in handoffs[0]["summary"]


def test_send_endpoint_primes_new_customer_sales_flow(monkeypatch):
    deliveries = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        deliveries.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "actions": actions,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)

    client = TestClient(app)
    seeded = client.post(
        "/send",
        json={
            "chanel": "whatsapp",
            "recipient": "593999",
            "session_id": "seed-sales-1",
            "odoo": "NUEVO_CLIENTE",
            "message": "inicio",
        },
    )
    assert seeded.status_code == 200
    assert deliveries[0]["channel"] == "whatsapp"
    assert "nombre completo" in deliveries[0]["message"].lower()

    resumed = client.post(
        "/v1/messages",
        json={
            "mensaje": "Juan Perez",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "seed-sales-1",
        },
    )
    assert resumed.status_code == 200
    body = resumed.json()
    assert body["agent"] == "sales"
    assert "dirección completa" in body["message"].lower()


def test_send_endpoint_with_image_base64_exposes_temp_media(monkeypatch):
    deliveries = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        deliveries.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "actions": actions,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)

    client = TestClient(app)
    response = client.post(
        "/send",
        json={
            "chanel": "whatsapp",
            "recipient": "593999",
            "tipo": "imagen",
            "filename": "demo.png",
            "mime_type": "image/png",
            "base64_data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z7mQAAAAASUVORK5CYII=",
        },
    )
    assert response.status_code == 200
    assert deliveries[0]["media_type"] == "image"
    assert deliveries[0]["message"].startswith("http://testserver/media/")

    media_path = deliveries[0]["message"].replace("http://testserver", "")
    media_response = client.get(media_path)
    assert media_response.status_code == 200
    assert media_response.headers["content-type"].startswith("image/png")
    assert len(media_response.content) > 0


def test_send_endpoint_requires_internal_secret_when_configured(monkeypatch):
    dispatched = []

    async def fake_dispatch(payload: dict):
        dispatched.append(payload)

    settings = get_settings()
    monkeypatch.setattr(settings, "bot_api_internal_secret", "internal-secret-123")
    monkeypatch.setattr(gateway_routes, "_dispatch_internal_send", fake_dispatch)

    client = TestClient(app)
    payload = {
        "chanel": "whatsapp",
        "recipient": "593999",
        "message": "hola",
    }

    rejected = client.post("/send", json=payload)
    assert rejected.status_code == 401
    assert rejected.json()["detail"] == "invalid_internal_secret"
    assert dispatched == []

    accepted = client.post("/send", json=payload, headers={"X-Internal-Secret": "internal-secret-123"})
    assert accepted.status_code == 200
    assert accepted.text == "Enviado"
    assert len(dispatched) == 1


def test_send_endpoint_with_whatsapp_media_id_resolves_to_temp_media(monkeypatch):
    deliveries = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        deliveries.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "actions": actions,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    async def fake_download(media_id: str, payload: dict):
        assert media_id == "1186454436736334"
        return "http://testserver/media/from-graph.png"

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(gateway_routes, "_download_whatsapp_media_reference", fake_download)

    client = TestClient(app)
    response = client.post(
        "/send",
        json={
            "chanel": "whatsapp",
            "recipient": "593999",
            "tipo": "imagen",
            "message": "1186454436736334",
        },
    )
    assert response.status_code == 200
    assert deliveries[0]["media_type"] == "image"
    assert deliveries[0]["message"] == "http://testserver/media/from-graph.png"


def test_send_endpoint_with_odoo_channel_attachment_resolves_to_temp_media(monkeypatch):
    deliveries = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        deliveries.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "actions": actions,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    async def fake_odoo_download(payload: dict):
        assert payload["channel_id"] == 7011
        assert payload["message_id"] == 12345
        return "http://testserver/media/from-odoo.png"

    async def fake_graph_download(media_id: str, payload: dict):
        return None

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(gateway_routes, "_download_odoo_attachment_reference", fake_odoo_download)
    monkeypatch.setattr(gateway_routes, "_download_whatsapp_media_reference", fake_graph_download)

    client = TestClient(app)
    response = client.post(
        "/send",
        json={
            "chanel": "whatsapp",
            "recipient": "593999",
            "tipo": "imagen",
            "message": "1452163789825211",
            "channel_id": 7011,
            "message_id": 12345,
        },
    )
    assert response.status_code == 200
    assert deliveries[0]["media_type"] == "image"
    assert deliveries[0]["message"] == "http://testserver/media/from-odoo.png"


def test_send_endpoint_with_text_payload_and_odoo_attachment_also_delivers_media(monkeypatch):
    deliveries = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        deliveries.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "actions": actions,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    async def fake_resolve_odoo_media(payload: dict, *, allow_channel_fallback: bool = True):
        assert payload["message_id"] == 54321
        assert allow_channel_fallback is True
        return "image", "http://testserver/media/from-odoo-caption.png"

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(gateway_routes, "_resolve_odoo_attachment_media", fake_resolve_odoo_media)

    client = TestClient(app)
    response = client.post(
        "/send",
        json={
            "chanel": "whatsapp",
            "recipient": "593999",
            "message": "esa es",
            "message_id": 54321,
            "channel_id": 7011,
            "internal_user": 17,
        },
    )
    assert response.status_code == 200
    assert deliveries == [
        {
            "channel": "whatsapp",
            "recipient": "593999",
            "message": "http://testserver/media/from-odoo-caption.png",
            "actions": None,
            "media_type": "image",
        },
        {
            "channel": "whatsapp",
            "recipient": "593999",
            "message": "esa es",
            "actions": None,
            "media_type": None,
        },
    ]


def test_channel_delivery_sends_whatsapp_audio_with_media_id(monkeypatch):
    captured = {}

    async def fake_post(self, url, *, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return {"status": "sent"}

    settings = get_settings()
    monkeypatch.setattr(settings, "url_wpp", "https://graph.facebook.com/v22.0/demo/messages")
    monkeypatch.setattr(settings, "token_whatsapp", "token-demo")
    monkeypatch.setattr(ChannelDeliveryService, "_post", fake_post)

    result = asyncio.run(
        ChannelDeliveryService().deliver(
            channel="whatsapp",
            recipient="593999",
            message="wamedia-audio-1",
            media_type="audio",
        )
    )

    assert result["status"] == "sent"
    assert captured["payload"]["type"] == "audio"
    assert captured["payload"]["audio"]["id"] == "wamedia-audio-1"


def test_channel_delivery_sends_whatsapp_document_with_link(monkeypatch):
    captured = {}

    async def fake_post(self, url, *, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return {"status": "sent"}

    settings = get_settings()
    monkeypatch.setattr(settings, "url_wpp", "https://graph.facebook.com/v22.0/demo/messages")
    monkeypatch.setattr(settings, "token_whatsapp", "token-demo")
    monkeypatch.setattr(ChannelDeliveryService, "_post", fake_post)

    result = asyncio.run(
        ChannelDeliveryService().deliver(
            channel="whatsapp",
            recipient="593999",
            message="https://example.com/proof.pdf",
            media_type="document",
        )
    )

    assert result["status"] == "sent"
    assert captured["payload"]["type"] == "document"
    assert captured["payload"]["document"]["link"] == "https://example.com/proof.pdf"


def test_send_endpoint_skips_empty_internal_text(monkeypatch):
    deliveries = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        deliveries.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)

    client = TestClient(app)
    response = client.post(
        "/send",
        json={
            "chanel": "whatsapp",
            "recipient": "593999",
            "message": "",
        },
    )
    assert response.status_code == 200
    assert deliveries == []


def test_send_endpoint_skips_unresolved_internal_media(monkeypatch):
    deliveries = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        deliveries.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    async def fake_odoo_download(payload: dict):
        return None

    async def fake_graph_download(media_id: str, payload: dict):
        return None

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(gateway_routes, "_download_odoo_attachment_reference", fake_odoo_download)
    monkeypatch.setattr(gateway_routes, "_download_whatsapp_media_reference", fake_graph_download)

    client = TestClient(app)
    response = client.post(
        "/send",
        json={
            "chanel": "whatsapp",
            "recipient": "593999",
            "tipo": "imagen",
            "message": "1452163789825211",
            "channel_id": 7011,
        },
    )
    assert response.status_code == 200
    assert deliveries == []


def test_root_webhook_relays_human_image_to_odoo(monkeypatch):
    text_relays = []
    attachment_relays = []

    class FakeRegistry:
        async def resolve_session(self, **kwargs):
            return {
                "status": "found",
                "exists": True,
                "session_id": "human-relay-image-1",
                "internal_user": 504,
                "channel_id": 7011,
                "group": "support",
                "human_active": True,
                "ai_active": False,
                "cedula": "0706772340",
            }

        async def touch_contact(self, **kwargs):
            return {"status": "updated", **kwargs}

    async def fake_normalize(_payload):
        return [
            InboundMessage(
                mensaje="Imagen enviada",
                channel="whatsapp",
                recipient="593999",
                session_id="ext-session",
                attachments=[
                    Attachment(
                        type="image",
                        mime_type="image/png",
                        filename="proof.png",
                        base64_data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z7mQAAAAASUVORK5CYII=",
                    )
                ],
                metadata={"message_type": "image"},
            )
        ]

    async def fail_handle_message(self, payload):
        raise AssertionError(f"AI no debería procesar el mensaje humano: {payload}")

    async def fail_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        raise AssertionError("No debería enviarse una respuesta de IA al cliente")

    async def fake_relay_text(
        self,
        *,
        channel,
        recipient,
        message,
        tipo="texto",
        cedula=None,
        origen=None,
        group=None,
        internal_user=None,
        channel_id=None,
    ):
        text_relays.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "tipo": tipo,
                "cedula": cedula,
                "group": group,
                "internal_user": internal_user,
                "channel_id": channel_id,
            }
        )
        return {"status": "sent"}

    async def fake_relay_attachments(
        self,
        *,
        channel,
        recipient,
        attachments,
        cedula=None,
        origen=None,
        group=None,
        internal_user=None,
        channel_id=None,
    ):
        attachment_relays.append(
            {
                "channel": channel,
                "recipient": recipient,
                "attachments": attachments,
                "cedula": cedula,
                "group": group,
                "internal_user": internal_user,
                "channel_id": channel_id,
            }
        )
        return [{"status": "sent"}]

    monkeypatch.setattr(gateway_routes, "build_contact_registry", lambda: FakeRegistry())
    monkeypatch.setattr(gateway_routes, "normalize_meta_event", fake_normalize)
    monkeypatch.setattr(OrchestratorService, "handle_message", fail_handle_message)
    monkeypatch.setattr(ChannelDeliveryService, "deliver", fail_deliver)
    monkeypatch.setattr(OdooChatClient, "relay_customer_message", fake_relay_text)
    monkeypatch.setattr(OdooChatClient, "relay_attachments", fake_relay_attachments)

    client = TestClient(app)
    response = client.post("/", json={"object": "whatsapp_business_account", "entry": [{}]})
    assert response.status_code == 200
    assert text_relays == []
    assert len(attachment_relays) == 1
    assert attachment_relays[0]["attachments"][0]["type"] == "image"
    assert attachment_relays[0]["attachments"][0]["url"].startswith("http://testserver/media/")

    media_path = attachment_relays[0]["attachments"][0]["url"].replace("http://testserver", "")
    media_response = client.get(media_path)
    assert media_response.status_code == 200
    assert media_response.headers["content-type"].startswith("image/png")

    state = asyncio.run(build_session_store().get("human-relay-image-1"))
    assert state is not None
    assert state.human_handoff is True
    assert state.current_intent == "human_handoff"


def test_root_webhook_relays_human_audio_to_odoo(monkeypatch):
    relays = []

    class FakeRegistry:
        async def resolve_session(self, **kwargs):
            return {
                "status": "found",
                "exists": True,
                "session_id": "human-relay-audio-1",
                "internal_user": 504,
                "channel_id": 7011,
                "group": "support",
                "human_active": True,
                "ai_active": False,
                "cedula": "0706772340",
            }

        async def touch_contact(self, **kwargs):
            return {"status": "updated", **kwargs}

    async def fake_normalize(_payload):
        return [
            InboundMessage(
                mensaje="Audio enviado",
                channel="whatsapp",
                recipient="593999",
                session_id="ext-session-audio",
                attachments=[
                    Attachment(
                        type="audio",
                        mime_type="audio/ogg",
                        filename="voice.ogg",
                        base64_data=base64.b64encode(b"fake-audio").decode("ascii"),
                    )
                ],
                metadata={"message_type": "audio"},
            )
        ]

    async def fail_handle_message(self, payload):
        raise AssertionError(f"AI no debería procesar el audio humano: {payload}")

    async def fake_relay(
        self,
        *,
        channel,
        recipient,
        message,
        tipo="texto",
        cedula=None,
        origen=None,
        group=None,
        internal_user=None,
        channel_id=None,
    ):
        relays.append({"channel": channel, "recipient": recipient, "message": message, "tipo": tipo})
        return {"status": "sent"}

    monkeypatch.setattr(gateway_routes, "build_contact_registry", lambda: FakeRegistry())
    monkeypatch.setattr(gateway_routes, "normalize_meta_event", fake_normalize)
    monkeypatch.setattr(OrchestratorService, "handle_message", fail_handle_message)
    monkeypatch.setattr(OdooChatClient, "relay_customer_message", fake_relay)

    client = TestClient(app)
    response = client.post("/", json={"object": "whatsapp_business_account", "entry": [{}]})
    assert response.status_code == 200
    assert len(relays) == 1
    assert relays[0]["tipo"] == "audio"
    assert relays[0]["message"].startswith("http://testserver/media/")

    state = asyncio.run(build_session_store().get("human-relay-audio-1"))
    assert state is not None
    assert state.human_handoff is True


def test_outbound_endpoint_stores_and_lists_messages():
    client = TestClient(app)

    created = client.post(
        "/v1/outbound",
        json={
            "session_id": "outbox-1",
            "chanel": "whatsapp",
            "recipient": "593999",
            "message": "Proceso terminado",
            "origen": "worker_ocr",
            "metadata": {"job_id": "job-1"},
        },
    )
    assert created.status_code == 200
    assert created.json()["status"] == "queued"

    listed = client.get("/v1/outbound", params={"session_id": "outbox-1"})
    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] >= 1
    assert any(item["message"] == "Proceso terminado" for item in body["items"])


def test_outbound_endpoint_requires_internal_secret_when_configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "bot_api_internal_secret", "internal-secret-123")

    client = TestClient(app)
    created = client.post(
        "/v1/outbound",
        json={
            "session_id": "outbox-auth-1",
            "chanel": "whatsapp",
            "recipient": "593999",
            "message": "Proceso protegido",
        },
    )
    assert created.status_code == 401
    assert created.json()["detail"] == "invalid_internal_secret"

    listed = client.get("/v1/outbound", params={"session_id": "outbox-auth-1"})
    assert listed.status_code == 401
    assert listed.json()["detail"] == "invalid_internal_secret"

    accepted = client.post(
        "/v1/outbound",
        headers={"Authorization": "Bearer internal-secret-123"},
        json={
            "session_id": "outbox-auth-1",
            "chanel": "whatsapp",
            "recipient": "593999",
            "message": "Proceso protegido",
        },
    )
    assert accepted.status_code == 200


def test_messages_endpoint_routes_sales():
    client = TestClient(app)
    response = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero contratar un plan",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "sales-1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "sales"
    assert body["intent"] == "commercial"
    assert "nombre completo" in body["message"].lower()


def test_messages_endpoint_requires_internal_secret_when_configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "bot_api_internal_secret", "internal-secret-123")

    client = TestClient(app)
    rejected = client.post(
        "/v1/messages",
        json={
            "mensaje": "Quiero contratar un plan",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "sales-auth-1",
        },
    )
    assert rejected.status_code == 401
    assert rejected.json()["detail"] == "invalid_internal_secret"

    accepted = client.post(
        "/v1/messages",
        headers={"X-Internal-Secret": "internal-secret-123"},
        json={
            "mensaje": "Quiero contratar un plan",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "sales-auth-1",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["agent"] == "sales"


def test_messages_endpoint_support_requires_identity():
    client = TestClient(app)
    response = client.post(
        "/v1/messages",
        json={
            "mensaje": "No tengo internet",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "support-1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "clarify"
    assert body["intent"] == "ask_cedula"
    assert "titular del contrato" in body["message"].lower()


def test_initial_greeting_is_conversational(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "assistant_name", "Daniela")
    monkeypatch.setattr(settings, "assistant_names", "Daniela,Daniel")

    client = TestClient(app)
    response = client.post(
        "/v1/messages",
        json={
            "mensaje": "Hola",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "hello-1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "clarify"
    lowered = body["message"].lower()
    assert "gonet" in lowered
    assert ("daniela" in lowered) or ("daniel" in lowered)
    assert any(
        marker in lowered
        for marker in ("te atiende", "soy ", "te saluda", "estás hablando con", "estas hablando con")
    )
    assert "soporte técnico" in lowered
    assert "virtual" not in body["message"].lower()


def test_repeated_greeting_does_not_reintroduce_assistant_name(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "assistant_name", "Daniel")
    monkeypatch.setattr(settings, "assistant_names", "Daniel")

    client = TestClient(app)
    session_id = "hello-repeat-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Hola",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert first.status_code == 200
    first_lowered = first.json()["message"].lower()
    assert "daniel" in first_lowered
    assert any(
        marker in first_lowered
        for marker in ("te atiende", "soy ", "te saluda", "estás hablando con", "estas hablando con")
    )

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "Hola",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    body = second.json()
    lowered = body["message"].lower()
    assert "soy daniel" not in lowered
    assert "daniel de gonet" not in lowered
    assert any(
        marker in lowered
        for marker in ("aquí sigo", "aqui sigo", "aquí estoy", "aqui estoy", "te leo", "cuéntame", "cuentame")
    )


def test_no_interest_message_gets_conversational_close(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "assistant_name", "Daniel")
    monkeypatch.setattr(settings, "assistant_names", "Daniel")

    client = TestClient(app)
    session_id = "no-interest-1"

    first = client.post(
        "/v1/messages",
        json={
            "mensaje": "Hola",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert first.status_code == 200

    second = client.post(
        "/v1/messages",
        json={
            "mensaje": "No quiero nada",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": session_id,
        },
    )
    assert second.status_code == 200
    body = second.json()
    assert "está bien" in body["message"].lower() or "esta bien" in body["message"].lower()
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


def test_out_of_scope_message_gets_scope_guardrail():
    client = TestClient(app)
    response = client.post(
        "/v1/messages",
        json={
            "mensaje": "Cuéntame un chiste de fútbol",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "scope-1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "clarify"
    assert "únicamente con soporte técnico" in body["message"].lower()


def test_too_long_message_gets_length_guardrail():
    client = TestClient(app)
    response = client.post(
        "/v1/messages",
        json={
            "mensaje": "hola " * 200,
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "long-1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "clarify"
    assert "mensaje corto y claro" in body["message"].lower()


def test_close_endpoint_clears_seeded_sales_session(monkeypatch):
    deliveries = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        deliveries.append(message)
        return {"status": "sent"}

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)

    client = TestClient(app)
    seeded = client.post(
        "/send",
        json={
            "chanel": "whatsapp",
            "recipient": "593999",
            "session_id": "close-sales-1",
            "odoo": "NUEVO_CLIENTE",
            "message": "inicio",
        },
    )
    assert seeded.status_code == 200

    before_close = client.post(
        "/v1/messages",
        json={
            "mensaje": "Juan Perez",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "close-sales-1",
        },
    )
    assert before_close.status_code == 200
    assert "dirección completa" in before_close.json()["message"].lower()

    closed = client.post("/close", json={"session_id": "close-sales-1"})
    assert closed.status_code == 200
    assert closed.json()["closed"] is True

    after_close = client.post(
        "/v1/messages",
        json={
            "mensaje": "Juan Perez",
            "channel": "whatsapp",
            "recipient": "593999",
            "session_id": "close-sales-1",
        },
    )
    assert after_close.status_code == 200
    assert "dirección completa" not in after_close.json()["message"].lower()


def test_close_endpoint_requires_internal_secret_when_configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "bot_api_internal_secret", "internal-secret-123")

    client = TestClient(app)
    rejected = client.post("/close", json={"session_id": "close-auth-1"})
    assert rejected.status_code == 401
    assert rejected.json()["detail"] == "invalid_internal_secret"

    accepted = client.post(
        "/close",
        headers={"X-Internal-Secret": "internal-secret-123"},
        json={"session_id": "close-auth-1"},
    )
    assert accepted.status_code == 200


def test_session_context_load_ignores_mismatched_session_owner():
    store = build_session_store()
    asyncio.run(
        store.set(
            SessionState(
                session_id="shared-session-1",
                channel="whatsapp",
                recipient="593111",
                current_intent="billing",
                last_agent="billing",
                cedula="0703042374001",
            )
        )
    )

    state = asyncio.run(
        SessionContextService().load(
            InboundMessage(
                mensaje="Hola",
                channel="whatsapp",
                recipient="593999",
                session_id="shared-session-1",
            )
        )
    )

    assert state.session_id == "shared-session-1"
    assert state.recipient == "593999"
    assert state.current_intent is None
    assert state.last_agent is None
    assert state.cedula is None


def test_inactivity_service_closes_human_handoff_session(monkeypatch):
    delivered = []
    odoo_notifications = []
    closed_contacts = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        delivered.append({"channel": channel, "recipient": recipient, "message": message})
        return {"status": "sent"}

    async def fake_notify(self, *, channel, recipient, cedula=None, group=None, origen=None):
        odoo_notifications.append(
            {
                "channel": channel,
                "recipient": recipient,
                "cedula": cedula,
                "group": group,
                "origen": origen,
            }
        )
        return {"status": "sent"}

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(OdooChatClient, "notify_channel_closed", fake_notify)
    registry = build_contact_registry()

    async def fake_close_contact(*, recipient=None, red=None, channel_id=None):
        closed_contacts.append({"recipient": recipient, "red": red, "channel_id": channel_id})
        return {"status": "closed", "recipient": recipient, "red": red}

    monkeypatch.setattr(registry, "close_contact", fake_close_contact)

    settings = get_settings()
    monkeypatch.setattr(settings, "time_inactive_chat", 1)

    sessions = SessionContextService()
    expired = SessionState(
        session_id="inactive-human-1",
        channel="whatsapp",
        recipient="593111",
        cedula="0102030405",
        current_intent="support",
        last_agent="handoff",
        human_handoff=True,
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    asyncio.run(sessions.save(expired))

    service = InactivityService(settings=settings, sessions=sessions)
    processed = asyncio.run(service.process_human_inactivity())
    assert processed == 1
    assert len(delivered) == 1
    assert "se va a proceder a cerrar" in delivered[0]["message"].lower()
    assert len(odoo_notifications) == 1
    assert odoo_notifications[0]["recipient"] == "593111"
    assert closed_contacts == [{"recipient": "593111", "red": "whatsapp", "channel_id": None}]
    remaining = asyncio.run(sessions.list_sessions())
    assert remaining == []


def test_inactivity_service_keeps_recent_ai_session_when_updated_at_is_stale(monkeypatch):
    delivered = []
    closed_contacts = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        delivered.append({"channel": channel, "recipient": recipient, "message": message})
        return {"status": "sent"}

    registry = build_contact_registry()

    async def fake_close_contact(*, recipient=None, red=None, channel_id=None):
        closed_contacts.append({"recipient": recipient, "red": red, "channel_id": channel_id})
        return {"status": "closed", "recipient": recipient, "red": red}

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(registry, "close_contact", fake_close_contact)

    settings = get_settings()
    monkeypatch.setattr(settings, "time_inactive_chat_ia", 10)

    now = datetime.now(timezone.utc)
    sessions = SessionContextService()
    active = SessionState(
        session_id="inactive-ia-stale-updated-1",
        channel="whatsapp",
        recipient="593222",
        current_intent="ask_cedula",
        last_agent="clarify",
        awaiting_field="cedula",
        updated_at=now - timedelta(minutes=30),
        last_user_message_at=now - timedelta(seconds=20),
        last_assistant_message_at=now - timedelta(seconds=10),
    )
    asyncio.run(sessions.save(active))

    service = InactivityService(settings=settings, sessions=sessions)
    processed = asyncio.run(service.process_ia_inactivity())
    assert processed == 0
    assert delivered == []
    assert closed_contacts == []

    remaining = asyncio.run(sessions.list_sessions())
    assert len(remaining) == 1
    assert remaining[0].session_id == "inactive-ia-stale-updated-1"
