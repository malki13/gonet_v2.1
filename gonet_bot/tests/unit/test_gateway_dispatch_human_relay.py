import asyncio

from apps.bot_api.routes.gateway_dispatch import (
    dispatch_internal_send,
    relay_human_message,
    resolve_runtime_session,
    should_relay_human_message,
)
from apps.bot_api.routes.gateway_media import (
    _map_inbound_attachment_tipo,
    _resolve_inbound_attachment_reference,
)
from packages.shared.config import get_settings
from packages.shared.schemas import Attachment, InboundMessage, SessionState


def test_relay_human_message_sends_image_as_attachment_payload(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "public_base_url", "https://bot.example")

    class FakeRegistry:
        def __init__(self):
            self.touched = []

        async def resolve_session(self, *, recipient, red):
            return {
                "status": "found",
                "exists": True,
                "session_id": "relay-human-image-1",
                "internal_user": 504,
                "channel_id": 7011,
                "group": "support",
                "human_active": True,
                "ai_active": False,
                "cedula": "0706772340",
            }

        async def touch_contact(self, **kwargs):
            self.touched.append(kwargs)
            return {"status": "updated"}

        async def mark_human_active(self, **kwargs):
            self.touched.append({"mark_human_active": kwargs})
            return {"status": "updated"}

    class FakeSessions:
        def __init__(self):
            self.state = SessionState(session_id="relay-human-image-1", cedula="0706772340")
            self.touches = []

        async def load(self, _message):
            return self.state

        async def save(self, state):
            self.state = state

        async def touch(self, **kwargs):
            self.touches.append(kwargs)

    class FakeOdooChat:
        def __init__(self):
            self.text_relays = []
            self.attachment_relays = []

        async def relay_customer_message(self, **kwargs):
            self.text_relays.append(kwargs)
            return {"status": "sent"}

        async def relay_attachments(self, **kwargs):
            self.attachment_relays.append(kwargs)
            return [{"status": "sent"}]

    registry = FakeRegistry()
    sessions = FakeSessions()
    odoo_chat = FakeOdooChat()
    message = InboundMessage(
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

    asyncio.run(
        relay_human_message(
            message,
            contact_registry=registry,
            sessions=sessions,
            odoo_chat=odoo_chat,
            logger_=__import__("logging").getLogger("test.gateway_dispatch"),
            map_inbound_attachment_tipo=_map_inbound_attachment_tipo,
            resolve_inbound_attachment_reference=_resolve_inbound_attachment_reference,
        )
    )

    assert odoo_chat.text_relays == []
    assert len(odoo_chat.attachment_relays) == 1
    relayed_attachment = odoo_chat.attachment_relays[0]["attachments"][0]
    assert relayed_attachment["type"] == "image"
    assert relayed_attachment["base64_data"]
    assert sessions.state.human_handoff is True
    assert sessions.state.current_intent == "human_handoff"
    assert any("mark_human_active" in item for item in registry.touched)


def test_relay_human_message_does_not_raise_when_attachment_relay_fails(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "public_base_url", "https://bot.example")

    class FakeRegistry:
        async def resolve_session(self, *, recipient, red):
            return {
                "status": "found",
                "exists": True,
                "session_id": "relay-human-image-fail-1",
                "internal_user": 504,
                "channel_id": 7011,
                "group": "support",
                "human_active": True,
                "ai_active": False,
                "cedula": "0706772340",
            }

        async def touch_contact(self, **kwargs):
            return {"status": "updated"}

        async def mark_human_active(self, **kwargs):
            return {"status": "updated"}

    class FakeSessions:
        def __init__(self):
            self.state = SessionState(session_id="relay-human-image-fail-1", cedula="0706772340")
            self.saved = []
            self.touches = []

        async def load(self, _message):
            return self.state

        async def save(self, state):
            self.state = state
            self.saved.append(state)

        async def touch(self, **kwargs):
            self.touches.append(kwargs)

    class FakeOdooChat:
        async def relay_customer_message(self, **kwargs):
            return {"status": "sent"}

        async def relay_attachments(self, **kwargs):
            raise RuntimeError("attachment relay failed")

    sessions = FakeSessions()
    message = InboundMessage(
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

    asyncio.run(
        relay_human_message(
            message,
            contact_registry=FakeRegistry(),
            sessions=sessions,
            odoo_chat=FakeOdooChat(),
            logger_=__import__("logging").getLogger("test.gateway_dispatch"),
            map_inbound_attachment_tipo=_map_inbound_attachment_tipo,
            resolve_inbound_attachment_reference=_resolve_inbound_attachment_reference,
        )
    )

    assert sessions.state.human_handoff is True
    assert sessions.state.current_intent == "human_handoff"
    assert sessions.touches


def test_resolve_runtime_session_preserves_legacy_human_handoff_state(monkeypatch):
    class FakeRegistry:
        async def resolve_session(self, *, recipient, red):
            return {
                "status": "found",
                "exists": True,
                "session_id": "stale-registry-session",
                "internal_user": 504,
                "channel_id": 7011,
                "group": "support",
                "human_active": False,
                "ai_active": False,
                "cedula": "0706772340",
            }

    class FakeSessions:
        def __init__(self):
            self.state = SessionState(
                session_id="legacy-session",
                channel="whatsapp",
                recipient="593999",
                human_handoff=False,
                current_intent="human_handoff",
                last_agent="handoff",
            )

        async def load(self, _message):
            return self.state

    message = InboundMessage(
        mensaje="seguimos",
        channel="whatsapp",
        recipient="593999",
        session_id="legacy-session",
    )

    resolved = asyncio.run(
        resolve_runtime_session(
            message,
            registry=FakeRegistry(),
            sessions=FakeSessions(),
            logger_=__import__("logging").getLogger("test.gateway_dispatch"),
        )
    )

    assert resolved.session_id == "legacy-session"


def test_should_relay_human_message_uses_legacy_handoff_state(monkeypatch):
    class FakeRegistry:
        async def resolve_session(self, *, recipient, red):
            return {
                "status": "found",
                "exists": True,
                "session_id": "stale-registry-session",
                "internal_user": 504,
                "channel_id": 7011,
                "group": "support",
                "human_active": False,
                "ai_active": False,
                "cedula": "0706772340",
            }

    class FakeSessions:
        def __init__(self):
            self.state = SessionState(
                session_id="legacy-session",
                channel="whatsapp",
                recipient="593999",
                human_handoff=False,
                current_intent="human_handoff",
                last_agent="handoff",
            )

        async def load(self, _message):
            return self.state

    message = InboundMessage(
        mensaje="seguimos",
        channel="whatsapp",
        recipient="593999",
        session_id="legacy-session",
    )

    should_relay = asyncio.run(
        should_relay_human_message(
            message,
            registry=FakeRegistry(),
            sessions=FakeSessions(),
        )
    )

    assert should_relay is True


def test_dispatch_internal_send_marks_human_active_when_assignment_present():
    assignments = []
    human_marks = []
    touches = []
    deliveries = []
    session_touches = []

    class FakeRegistry:
        async def assign_odoo_channel(self, **kwargs):
            assignments.append(kwargs)
            return {"status": "updated"}

        async def mark_human_active(self, **kwargs):
            human_marks.append(kwargs)
            return {"status": "updated"}

        async def touch_contact(self, **kwargs):
            touches.append(kwargs)
            return {"status": "updated"}

    class FakeSessions:
        async def touch(self, **kwargs):
            session_touches.append(kwargs)

    class FakeDelivery:
        async def deliver(self, *, channel, recipient, message, actions=None, media_type=None):
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

    async def fake_prime_sales_capture(_payload):
        return None

    async def fake_resolve_internal_media_reference(payload, *, channel, media_type):
        return payload.get("message")

    async def fake_resolve_internal_attachment_media(payload, *, channel):
        return None, None

    asyncio.run(
        dispatch_internal_send(
            {
                "chanel": "whatsapp",
                "recipient": "593999",
                "message": "Hola desde Odoo",
                "internal_user": 17,
                "channel_id": 88,
                "group": "support",
            },
            delivery=FakeDelivery(),
            odoo_chat=object(),
            contact_registry=FakeRegistry(),
            sessions=FakeSessions(),
            logger_=__import__("logging").getLogger("test.gateway_dispatch"),
            preview_fn=lambda text: text,
            prime_sales_capture_session_cb=fake_prime_sales_capture,
            map_internal_media_type=lambda tipo: None,
            resolve_internal_media_reference_cb=fake_resolve_internal_media_reference,
            resolve_internal_attachment_media_cb=fake_resolve_internal_attachment_media,
        )
    )

    assert deliveries == [
        {
            "channel": "whatsapp",
            "recipient": "593999",
            "message": "Hola desde Odoo",
            "actions": None,
            "media_type": None,
        }
    ]
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
    assert len(touches) == 1
    assert session_touches == [
        {
            "session_id": None,
            "recipient": "593999",
            "channel": "whatsapp",
            "actor": "assistant",
            "human_handoff": True,
        }
    ]


def test_dispatch_internal_send_delivers_attachment_from_odoo_message_when_text_payload_has_attachment():
    deliveries = []

    class FakeRegistry:
        async def assign_odoo_channel(self, **kwargs):
            return {"status": "updated", **kwargs}

        async def mark_human_active(self, **kwargs):
            return {"status": "updated", **kwargs}

        async def touch_contact(self, **kwargs):
            return {"status": "updated", **kwargs}

    class FakeSessions:
        async def touch(self, **kwargs):
            return kwargs

    class FakeDelivery:
        async def deliver(self, *, channel, recipient, message, actions=None, media_type=None):
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

    async def fake_prime_sales_capture(_payload):
        return None

    async def fake_resolve_internal_media_reference(payload, *, channel, media_type):
        return payload.get("message")

    async def fake_resolve_internal_attachment_media(payload, *, channel):
        assert payload["message_id"] == 9917
        assert channel == "whatsapp"
        return "image", "http://testserver/media/from-odoo.png"

    asyncio.run(
        dispatch_internal_send(
            {
                "chanel": "whatsapp",
                "recipient": "593999",
                "message": "esa es",
                "message_id": 9917,
                "channel_id": 88,
                "internal_user": 17,
                "group": "support",
            },
            delivery=FakeDelivery(),
            odoo_chat=object(),
            contact_registry=FakeRegistry(),
            sessions=FakeSessions(),
            logger_=__import__("logging").getLogger("test.gateway_dispatch"),
            preview_fn=lambda text: text,
            prime_sales_capture_session_cb=fake_prime_sales_capture,
            map_internal_media_type=lambda tipo: None,
            resolve_internal_media_reference_cb=fake_resolve_internal_media_reference,
            resolve_internal_attachment_media_cb=fake_resolve_internal_attachment_media,
        )
    )

    assert deliveries == [
        {
            "channel": "whatsapp",
            "recipient": "593999",
            "message": "http://testserver/media/from-odoo.png",
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
