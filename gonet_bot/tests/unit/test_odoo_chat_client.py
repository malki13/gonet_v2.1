import asyncio
import base64

from packages.integrations.odoo_chat import OdooChatClient


def test_odoo_chat_client_relays_attachments_as_media_messages(monkeypatch):
    sent = []

    async def fake_send_direct(
        self,
        *,
        channel,
        recipient,
        message,
        cedula=None,
        origen=None,
        group=None,
        tipo="texto",
        internal_user=None,
        channel_id=None,
        extra_payload=None,
    ):
        sent.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "tipo": tipo,
                "extra_payload": extra_payload,
            }
        )
        return {"status": "sent"}

    monkeypatch.setattr(OdooChatClient, "_send_direct_odoo_chat", fake_send_direct)

    client = OdooChatClient()
    result = asyncio.run(
        client.relay_attachments(
            channel="whatsapp",
            recipient="593999",
            attachments=[
                {
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "filename": "proof.jpg",
                    "base64_data": base64.b64encode(b"fake-image").decode("ascii"),
                }
            ],
            cedula="0102030405",
            origen="ia",
        )
    )

    assert len(result) == 1
    assert sent[0]["tipo"] == "imagen"
    assert sent[0]["message"].startswith("http")
    assert sent[0]["extra_payload"]["media_ref"].startswith("http")
    assert sent[0]["extra_payload"]["filename"] == "proof.jpg"
    assert sent[0]["extra_payload"]["base64_data"]


def test_odoo_chat_client_relays_attachments_using_resolved_assignment(monkeypatch):
    sent = []

    async def fake_send_direct(
        self,
        *,
        channel,
        recipient,
        message,
        cedula=None,
        origen=None,
        group=None,
        tipo="texto",
        internal_user=None,
        channel_id=None,
        extra_payload=None,
    ):
        sent.append(
            {
                "internal_user": internal_user,
                "channel_id": channel_id,
                "group": group,
                "tipo": tipo,
                "extra_payload": extra_payload,
            }
        )
        return {"status": "sent"}

    class FakeRegistry:
        async def resolve_session(self, *, recipient, red):
            return {
                "internal_user": 77,
                "channel_id": 7011,
                "group": "support",
            }

    monkeypatch.setattr(OdooChatClient, "_send_direct_odoo_chat", fake_send_direct)
    monkeypatch.setattr("packages.integrations.odoo_chat.build_contact_registry", lambda: FakeRegistry())

    client = OdooChatClient()
    asyncio.run(
        client.relay_attachments(
            channel="whatsapp",
            recipient="593999",
            attachments=[
                {
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "filename": "proof.jpg",
                    "base64_data": base64.b64encode(b"fake-image").decode("ascii"),
                }
            ],
            cedula="0102030405",
            origen="ia",
        )
    )

    assert sent[0]["tipo"] == "imagen"
    assert sent[0]["internal_user"] == 77
    assert sent[0]["channel_id"] == 7011
    assert sent[0]["group"] == "support"


def test_odoo_chat_client_prefers_proxy_url_when_base64_media_is_available(monkeypatch):
    sent = []

    async def fake_send_direct(
        self,
        *,
        channel,
        recipient,
        message,
        cedula=None,
        origen=None,
        group=None,
        tipo="texto",
        internal_user=None,
        channel_id=None,
        extra_payload=None,
    ):
        sent.append({"message": message, "extra_payload": extra_payload})
        return {"status": "sent"}

    monkeypatch.setattr(OdooChatClient, "_send_direct_odoo_chat", fake_send_direct)
    monkeypatch.setattr("packages.integrations.odoo_chat.store_temp_base64_media", lambda *args, **kwargs: "proof-token.jpg")
    monkeypatch.setattr("packages.integrations.odoo_chat.build_public_media_url", lambda token: f"https://bot.example/media/{token}")

    client = OdooChatClient()
    asyncio.run(
        client.relay_attachments(
            channel="whatsapp",
            recipient="593999",
            attachments=[
                {
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "filename": "proof.jpg",
                    "base64_data": base64.b64encode(b"fake-image").decode("ascii"),
                }
            ],
            cedula="0102030405",
            origen="ia",
        )
    )

    assert sent[0]["message"] == "https://bot.example/media/proof-token.jpg"
    assert sent[0]["extra_payload"]["media_ref"] == "https://bot.example/media/proof-token.jpg"
    assert sent[0]["extra_payload"]["filename"] == "proof.jpg"
    assert sent[0]["extra_payload"]["base64_data"]


def test_odoo_chat_client_prefers_existing_attachment_url_over_proxy(monkeypatch):
    sent = []

    async def fake_send_direct(
        self,
        *,
        channel,
        recipient,
        message,
        cedula=None,
        origen=None,
        group=None,
        tipo="texto",
        internal_user=None,
        channel_id=None,
        extra_payload=None,
    ):
        sent.append({"message": message, "extra_payload": extra_payload})
        return {"status": "sent"}

    monkeypatch.setattr(OdooChatClient, "_send_direct_odoo_chat", fake_send_direct)
    monkeypatch.setattr("packages.integrations.odoo_chat.store_temp_base64_media", lambda *args, **kwargs: "proof-token.jpg")
    monkeypatch.setattr("packages.integrations.odoo_chat.build_public_media_url", lambda token: f"https://bot.example/media/{token}")

    client = OdooChatClient()
    asyncio.run(
        client.relay_attachments(
            channel="whatsapp",
            recipient="593999",
            attachments=[
                {
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "filename": "proof.jpg",
                    "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123",
                    "base64_data": base64.b64encode(b"fake-image").decode("ascii"),
                }
            ],
            cedula="0102030405",
            origen="ia",
        )
    )

    assert sent[0]["message"] == "https://bot.example/media/proof-token.jpg"
    assert sent[0]["extra_payload"]["media_ref"] == "https://bot.example/media/proof-token.jpg"


def test_odoo_chat_client_uses_original_url_when_no_base64_media_exists(monkeypatch):
    sent = []

    async def fake_send_direct(
        self,
        *,
        channel,
        recipient,
        message,
        cedula=None,
        origen=None,
        group=None,
        tipo="texto",
        internal_user=None,
        channel_id=None,
        extra_payload=None,
    ):
        sent.append({"message": message, "extra_payload": extra_payload})
        return {"status": "sent"}

    monkeypatch.setattr(OdooChatClient, "_send_direct_odoo_chat", fake_send_direct)

    client = OdooChatClient()
    asyncio.run(
        client.relay_attachments(
            channel="whatsapp",
            recipient="593999",
            attachments=[
                {
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "filename": "proof.jpg",
                    "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123",
                }
            ],
            cedula="0102030405",
            origen="ia",
        )
    )

    assert sent[0]["message"] == "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123"
    assert sent[0]["extra_payload"]["media_ref"] == "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123"


def test_odoo_chat_client_drops_partial_assignment_in_payload(monkeypatch):
    payloads = []

    async def fake_post_json(self, target_url, payload):
        payloads.append(payload)
        return {"status": "sent"}

    client = OdooChatClient()
    monkeypatch.setattr(client, "_post_json", fake_post_json.__get__(client, OdooChatClient))
    monkeypatch.setattr(client.settings, "url_odoo_chat", "http://odoo.test/api/public_chat")

    asyncio.run(
        client._send_direct_odoo_chat(
            channel="whatsapp",
            recipient="593999",
            message="https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123",
            cedula="0102030405",
            origen="ia",
            tipo="imagen",
            internal_user=None,
            channel_id=531,
        )
    )

    assert "internal_user" not in payloads[0]
    assert payloads[0]["channel_id"] == 531


def test_odoo_chat_client_uses_recipient_as_vat_fallback_when_cedula_is_missing(monkeypatch):
    payloads = []

    async def fake_post_json(self, target_url, payload):
        payloads.append(payload)
        return {"status": "sent"}

    client = OdooChatClient()
    monkeypatch.setattr(client, "_post_json", fake_post_json.__get__(client, OdooChatClient))
    monkeypatch.setattr(client.settings, "url_odoo_chat", "http://odoo.test/api/public_chat")

    asyncio.run(
        client._send_direct_odoo_chat(
            channel="whatsapp",
            recipient="593961588185",
            message="Fallo del sistema durante el procesamiento o envío del mensaje.",
            cedula=None,
            origen="ia",
            group="support",
        )
    )

    assert payloads[0]["vat"] == "593961588185"


def test_odoo_chat_client_discards_partial_registry_assignment(monkeypatch):
    sent = []

    async def fake_send_direct(
        self,
        *,
        channel,
        recipient,
        message,
        cedula=None,
        origen=None,
        group=None,
        tipo="texto",
        internal_user=None,
        channel_id=None,
        extra_payload=None,
    ):
        sent.append(
            {
                "internal_user": internal_user,
                "channel_id": channel_id,
                "group": group,
                "tipo": tipo,
            }
        )
        return {"status": "sent"}

    class FakeRegistry:
        async def resolve_session(self, *, recipient, red):
            return {
                "internal_user": None,
                "channel_id": 531,
                "group": "support",
            }

    monkeypatch.setattr(OdooChatClient, "_send_direct_odoo_chat", fake_send_direct)
    monkeypatch.setattr("packages.integrations.odoo_chat.build_contact_registry", lambda: FakeRegistry())

    client = OdooChatClient()
    asyncio.run(
        client.relay_attachments(
            channel="whatsapp",
            recipient="593999",
            attachments=[
                {
                    "type": "image",
                    "mime_type": "image/jpeg",
                    "filename": "proof.jpg",
                    "base64_data": base64.b64encode(b"fake-image").decode("ascii"),
                }
            ],
            cedula="0102030405",
            origen="ia",
        )
    )

    assert sent[0]["tipo"] == "imagen"
    assert sent[0]["internal_user"] is None
    assert sent[0]["channel_id"] == 531
    assert sent[0]["group"] == "support"


def test_odoo_chat_client_raises_on_json_error_body(monkeypatch):
    client = OdooChatClient()

    async def fake_post(_url, json):
        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "jsonrpc": "2.0",
                    "error": {
                        "message": "Odoo Server Error",
                        "data": {"message": "invalid input syntax for type integer"},
                    },
                }

        return FakeResponse()

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            return await fake_post(url, json)

    monkeypatch.setattr("packages.integrations.odoo_chat.httpx.AsyncClient", FakeAsyncClient)

    try:
        asyncio.run(
            client._post_json(
                "http://odoo.test/api/public_chat",
                {
                    "recipient": "593999",
                    "chanel": "whatsapp",
                    "group": "support",
                    "tipo": "imagen",
                    "message": "https://bot.example/media/proof.jpg",
                },
            )
        )
        raise AssertionError("Expected RuntimeError")
    except RuntimeError as exc:
        assert "invalid input syntax for type integer" in str(exc)
