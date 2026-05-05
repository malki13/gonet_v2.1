import asyncio
import base64
import logging

from packages.integrations.speech_to_text import SpeechToTextService
from packages.integrations.text_to_speech import TextToSpeechService
from packages.integrations.redis_store import build_session_store
from packages.orchestrator.service import OrchestratorService
from packages.orchestrator.session_context import SessionContextService
from apps.bot_api.routes import gateway_dispatch
from packages.shared.config import get_settings
from packages.shared.schemas import Attachment, InboundMessage, OutboundMessage, SessionState


def test_orchestrator_preprocesses_audio_to_transcript(monkeypatch):
    async def fake_transcribe(self, attachment):
        return {
            "status": "ok",
            "engine": "fake_stt",
            "text": "quiero contratar internet",
            "mime_type": "audio/ogg",
            "filename": "voice.ogg",
        }

    settings = get_settings()
    monkeypatch.setattr(settings, "audio_enabled", True)
    monkeypatch.setattr(SpeechToTextService, "transcribe_attachment", fake_transcribe)

    service = OrchestratorService()
    processed = asyncio.run(
        service._maybe_preprocess_audio_message(
            InboundMessage(
                mensaje="Audio enviado",
                channel="whatsapp",
                recipient="593999",
                session_id="audio-preprocess-1",
                attachments=[
                    Attachment(
                        type="audio",
                        mime_type="audio/ogg",
                        filename="voice.ogg",
                        base64_data=base64.b64encode(b"fake-audio").decode("ascii"),
                    )
                ],
            )
        )
    )

    assert processed.mensaje == "quiero contratar internet"
    assert processed.metadata["audio"]["input"] is True
    assert processed.metadata["audio"]["transcribed"] is True
    assert processed.metadata["audio"]["stt_engine"] == "fake_stt"


def test_gateway_process_and_deliver_replies_with_audio_when_enabled(monkeypatch):
    captured = {}

    class FakeTTS:
        async def synthesize(self, text: str, *, metadata=None) -> dict:
            captured["text"] = text
            captured["metadata"] = metadata
            return {
                "status": "ok",
                "engine": "fake_tts",
                "media_bytes": b"audio-reply",
                "mime_type": "audio/ogg",
                "filename": "reply.ogg",
                "voice": "es-EC-AndreaNeural",
            }

    class FakeOrchestrator:
        async def handle_message(self, message):
            return OutboundMessage(
                status="ok",
                message="Respuesta en voz",
                agent="sales",
                intent="commercial",
                confidence=0.95,
                metadata={
                    "audio": {"input": True, "transcribed": True},
                    "assistant_profile": {"display_name": "Daniela", "voice_gender": "female"},
                },
            )

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

    async def fake_escalate_new_client(*args, **kwargs):
        return {"status": "sent"}

    settings = get_settings()
    monkeypatch.setattr(settings, "audio_enabled", True)
    monkeypatch.setattr(settings, "audio_reply_mode", "same_as_input")
    monkeypatch.setattr(settings, "public_base_url", "https://bot.example")
    monkeypatch.setattr(gateway_dispatch, "_build_text_to_speech_service", lambda: FakeTTS())

    delivery = type("FakeDelivery", (), {"deliver": fake_deliver})()
    odoo_chat = type("FakeOdooChat", (), {"escalate_new_client": fake_escalate_new_client})()

    asyncio.run(
        gateway_dispatch.process_and_deliver(
            InboundMessage(
                mensaje="Audio enviado",
                channel="whatsapp",
                recipient="593999",
                session_id="audio-reply-1",
                attachments=[
                    Attachment(
                        type="audio",
                        mime_type="audio/ogg",
                        filename="voice.ogg",
                        base64_data=base64.b64encode(b"fake-audio").decode("ascii"),
                    )
                ],
            ),
            FakeOrchestrator(),
            delivery=delivery,
            odoo_chat=odoo_chat,
            logger_=logging.getLogger("test.audio"),
            preview_fn=lambda text: text,
        )
    )

    assert len(deliveries) == 1
    assert deliveries[0]["media_type"] == "audio"
    assert deliveries[0]["actions"] is None
    assert deliveries[0]["message"].startswith("https://bot.example/media/")
    assert captured["metadata"]["assistant_profile"]["voice_gender"] == "female"


def test_gateway_process_and_deliver_keeps_text_reply_when_audio_disabled(monkeypatch):
    class FakeTTS:
        async def synthesize(self, text: str, *, metadata=None) -> dict:
            raise AssertionError("TTS should not run when audio is disabled")

    class FakeOrchestrator:
        async def handle_message(self, message):
            return OutboundMessage(
                status="ok",
                message="Respuesta de texto",
                agent="sales",
                intent="commercial",
                confidence=0.95,
                metadata={"audio": {"input": True, "transcribed": True}},
            )

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

    async def fake_escalate_new_client(*args, **kwargs):
        return {"status": "sent"}

    settings = get_settings()
    monkeypatch.setattr(settings, "audio_enabled", False)
    monkeypatch.setattr(settings, "audio_reply_mode", "same_as_input")
    monkeypatch.setattr(gateway_dispatch, "_build_text_to_speech_service", lambda: FakeTTS())

    delivery = type("FakeDelivery", (), {"deliver": fake_deliver})()
    odoo_chat = type("FakeOdooChat", (), {"escalate_new_client": fake_escalate_new_client})()

    asyncio.run(
        gateway_dispatch.process_and_deliver(
            InboundMessage(
                mensaje="Audio enviado",
                channel="whatsapp",
                recipient="593999",
                session_id="audio-disabled-1",
                attachments=[
                    Attachment(
                        type="audio",
                        mime_type="audio/ogg",
                        filename="voice.ogg",
                        base64_data=base64.b64encode(b"fake-audio").decode("ascii"),
                    )
                ],
            ),
            FakeOrchestrator(),
            delivery=delivery,
            odoo_chat=odoo_chat,
            logger_=logging.getLogger("test.audio"),
            preview_fn=lambda text: text,
        )
    )

    assert len(deliveries) == 1
    assert deliveries[0]["media_type"] is None
    assert deliveries[0]["actions"] is None
    assert deliveries[0]["message"] == "Respuesta de texto"


def test_gateway_process_and_deliver_skips_delivery_when_response_requests_silence(monkeypatch):
    class FakeOrchestrator:
        async def handle_message(self, message):
            return OutboundMessage(
                status="ok",
                message="",
                agent="billing",
                intent="billing_processing_async",
                confidence=0.95,
                metadata={"skip_delivery": True},
            )

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

    async def fake_escalate_new_client(*args, **kwargs):
        return {"status": "sent"}

    delivery = type("FakeDelivery", (), {"deliver": fake_deliver})()
    odoo_chat = type("FakeOdooChat", (), {"escalate_new_client": fake_escalate_new_client})()

    asyncio.run(
        gateway_dispatch.process_and_deliver(
            InboundMessage(
                mensaje="¿Ya revisaron mi comprobante?",
                channel="whatsapp",
                recipient="593999",
                session_id="audio-skip-1",
            ),
            FakeOrchestrator(),
            delivery=delivery,
            odoo_chat=odoo_chat,
            logger_=logging.getLogger("test.audio"),
            preview_fn=lambda text: text,
        )
    )

    assert deliveries == []


def test_gateway_coalesces_close_text_messages_into_one_turn(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    build_session_store.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "inbound_coalescing_enabled", True)
    monkeypatch.setattr(settings, "inbound_coalescing_window_seconds", 0.03)
    monkeypatch.setattr(settings, "inbound_coalescing_max_messages", 8)

    handled = []
    deliveries = []

    class FakeOrchestrator:
        async def handle_message(self, message):
            handled.append(message)
            return OutboundMessage(
                status="ok",
                message=f"Procesado: {message.mensaje}",
                agent="support",
                intent="support",
                confidence=0.95,
                metadata=message.metadata,
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

    async def fake_escalate_new_client(*args, **kwargs):
        return {"status": "sent"}

    sessions = SessionContextService()
    delivery = type("FakeDelivery", (), {"deliver": fake_deliver})()
    odoo_chat = type("FakeOdooChat", (), {"escalate_new_client": fake_escalate_new_client})()
    orchestrator = FakeOrchestrator()

    async def run():
        first = InboundMessage(
            mensaje="hola",
            channel="whatsapp",
            recipient="593999",
            session_id="coalesce-1",
            metadata={"message_id": "m-1", "message_type": "text"},
        )
        second = InboundMessage(
            mensaje="el internet no sirve",
            channel="whatsapp",
            recipient="593999",
            session_id="coalesce-1",
            metadata={"message_id": "m-2", "message_type": "text"},
        )
        task1 = asyncio.create_task(
            gateway_dispatch.coalesce_and_process(
                first,
                orchestrator,
                sessions=sessions,
                delivery=delivery,
                odoo_chat=odoo_chat,
                logger_=logging.getLogger("test.coalesce"),
                preview_fn=lambda text: text,
            )
        )
        await asyncio.sleep(0.01)
        task2 = asyncio.create_task(
            gateway_dispatch.coalesce_and_process(
                second,
                orchestrator,
                sessions=sessions,
                delivery=delivery,
                odoo_chat=odoo_chat,
                logger_=logging.getLogger("test.coalesce"),
                preview_fn=lambda text: text,
            )
        )
        await asyncio.gather(task1, task2)

    asyncio.run(run())

    assert len(handled) == 1
    assert handled[0].mensaje == "hola\nel internet no sirve"
    assert handled[0].metadata["coalesced"] is True
    assert handled[0].metadata["coalesced_count"] == 2
    assert handled[0].metadata["coalesced_message_ids"] == ["m-1", "m-2"]
    assert len(deliveries) == 1
    assert deliveries[0]["message"] == "Procesado: hola\nel internet no sirve"


def test_gateway_flushes_buffer_when_attachment_arrives(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    build_session_store.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "inbound_coalescing_enabled", True)
    monkeypatch.setattr(settings, "inbound_coalescing_window_seconds", 0.05)
    monkeypatch.setattr(settings, "inbound_coalescing_max_messages", 8)

    handled = []
    deliveries = []

    class FakeOrchestrator:
        async def handle_message(self, message):
            handled.append(message)
            return OutboundMessage(
                status="ok",
                message=message.mensaje,
                agent="billing",
                intent="billing_proof",
                confidence=0.95,
                metadata=message.metadata,
            )

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        deliveries.append(message)
        return {"status": "sent"}

    async def fake_escalate_new_client(*args, **kwargs):
        return {"status": "sent"}

    sessions = SessionContextService()
    delivery = type("FakeDelivery", (), {"deliver": fake_deliver})()
    odoo_chat = type("FakeOdooChat", (), {"escalate_new_client": fake_escalate_new_client})()
    orchestrator = FakeOrchestrator()

    async def run():
        text_message = InboundMessage(
            mensaje="ya pague",
            channel="whatsapp",
            recipient="593998",
            session_id="coalesce-attachment-1",
            metadata={"message_id": "a-1", "message_type": "text"},
        )
        image_message = InboundMessage(
            mensaje="Documento enviado",
            channel="whatsapp",
            recipient="593998",
            session_id="coalesce-attachment-1",
            attachments=[Attachment(type="image", filename="proof.jpg", base64_data="ZmFrZQ==")],
            metadata={"message_id": "a-2", "message_type": "image"},
        )
        task1 = asyncio.create_task(
            gateway_dispatch.coalesce_and_process(
                text_message,
                orchestrator,
                sessions=sessions,
                delivery=delivery,
                odoo_chat=odoo_chat,
                logger_=logging.getLogger("test.coalesce"),
                preview_fn=lambda text: text,
            )
        )
        await asyncio.sleep(0.01)
        await gateway_dispatch.coalesce_and_process(
            image_message,
            orchestrator,
            sessions=sessions,
            delivery=delivery,
            odoo_chat=odoo_chat,
            logger_=logging.getLogger("test.coalesce"),
            preview_fn=lambda text: text,
        )
        await task1

    asyncio.run(run())

    assert len(handled) == 1
    assert handled[0].mensaje == "ya pague\nDocumento enviado"
    assert len(handled[0].attachments) == 1
    assert handled[0].metadata["coalesced"] is True
    assert deliveries == ["ya pague\nDocumento enviado"]


def test_gateway_coalesces_attachment_then_identity_text_into_one_turn(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    build_session_store.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "inbound_coalescing_enabled", True)
    monkeypatch.setattr(settings, "inbound_coalescing_window_seconds", 0.05)
    monkeypatch.setattr(settings, "inbound_coalescing_max_messages", 8)

    handled = []
    deliveries = []

    class FakeOrchestrator:
        async def handle_message(self, message):
            handled.append(message)
            return OutboundMessage(
                status="ok",
                message=f"Procesado: {message.mensaje}",
                agent="billing",
                intent="billing_proof",
                confidence=0.95,
                metadata=message.metadata,
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

    async def fake_escalate_new_client(*args, **kwargs):
        return {"status": "sent"}

    sessions = SessionContextService()
    delivery = type("FakeDelivery", (), {"deliver": fake_deliver})()
    odoo_chat = type("FakeOdooChat", (), {"escalate_new_client": fake_escalate_new_client})()
    orchestrator = FakeOrchestrator()

    async def run():
        first = InboundMessage(
            mensaje="Imagen enviada",
            channel="whatsapp",
            recipient="593998",
            session_id="coalesce-attachment-first-1",
            attachments=[Attachment(type="image", filename="proof.jpg", base64_data="ZmFrZQ==")],
            metadata={"message_id": "b-1", "message_type": "image"},
        )
        second = InboundMessage(
            mensaje="0102030405",
            channel="whatsapp",
            recipient="593998",
            session_id="coalesce-attachment-first-1",
            metadata={"message_id": "b-2", "message_type": "text"},
        )
        task1 = asyncio.create_task(
            gateway_dispatch.coalesce_and_process(
                first,
                orchestrator,
                sessions=sessions,
                delivery=delivery,
                odoo_chat=odoo_chat,
                logger_=logging.getLogger("test.coalesce"),
                preview_fn=lambda text: text,
            )
        )
        await asyncio.sleep(0.01)
        task2 = asyncio.create_task(
            gateway_dispatch.coalesce_and_process(
                second,
                orchestrator,
                sessions=sessions,
                delivery=delivery,
                odoo_chat=odoo_chat,
                logger_=logging.getLogger("test.coalesce"),
                preview_fn=lambda text: text,
            )
        )
        await asyncio.gather(task1, task2)

    asyncio.run(run())

    assert len(handled) == 1
    assert handled[0].mensaje == "Imagen enviada\n0102030405"
    assert len(handled[0].attachments) == 1
    assert handled[0].metadata["coalesced"] is True
    assert handled[0].metadata["coalesced_count"] == 2
    assert handled[0].metadata["coalesced_message_ids"] == ["b-1", "b-2"]
    assert len(deliveries) == 1
    assert deliveries[0]["message"] == "Procesado: Imagen enviada\n0102030405"


def test_text_to_speech_uses_female_voice_for_daniela(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "audio_tts_voice", "es-EC-LuisNeural")
    monkeypatch.setattr(settings, "audio_tts_voice_female", "es-EC-AndreaNeural")
    monkeypatch.setattr(settings, "audio_tts_voice_male", "es-EC-LuisNeural")

    service = TextToSpeechService()
    voice = service._resolve_voice({"assistant_profile": {"display_name": "Daniela", "voice_gender": "female"}})

    assert voice == "es-EC-AndreaNeural"


def test_orchestrator_clarify_result_includes_voice_profile(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "assistant_name", "Daniela")
    monkeypatch.setattr(settings, "assistant_names", "Daniela")

    service = OrchestratorService()
    state = SessionState(session_id="clarify-voice-1", channel="whatsapp", recipient="593999")
    result = service._build_clarify_result("insufficient_signal", state)

    assert result.metadata["assistant_profile"]["display_name"] == "Daniela"
    assert result.metadata["assistant_profile"]["voice_gender"] == "female"
