import asyncio
import subprocess

import httpx

from packages.channels.delivery import ChannelDeliveryService
from packages.shared.config import get_settings


def test_channel_delivery_uploads_local_proxy_image_before_send(monkeypatch, tmp_path):
    captured = {}
    upload_calls = []
    file_path = tmp_path / "proof.png"
    file_path.write_bytes(b"fake-image")

    async def fake_post(self, url, *, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return {"status": "sent"}

    class FakeUploadResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "wamedia-image-1"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, files=None):
            upload_calls.append({"url": url, "headers": headers, "files": files})
            return FakeUploadResponse()

    settings = get_settings()
    monkeypatch.setattr(settings, "url_wpp", "https://graph.facebook.com/v22.0/demo/messages")
    monkeypatch.setattr(settings, "token_whatsapp", "token-demo")
    monkeypatch.setattr(ChannelDeliveryService, "_post", fake_post)
    monkeypatch.setattr(
        "packages.channels.delivery.resolve_temp_media",
        lambda token: (file_path, {"mime_type": "image/png", "filename": "proof.png"}),
    )
    monkeypatch.setattr("packages.channels.delivery.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        ChannelDeliveryService().deliver(
            channel="whatsapp",
            recipient="593999",
            message="https://bot.example/media/proxy-token.png",
            media_type="image",
        )
    )

    assert result["status"] == "sent"
    assert upload_calls[0]["url"] == "https://graph.facebook.com/v22.0/demo/media"
    assert captured["payload"]["type"] == "image"
    assert captured["payload"]["image"]["id"] == "wamedia-image-1"


def test_channel_delivery_uploads_local_proxy_audio_before_send(monkeypatch, tmp_path):
    captured = {}
    upload_calls = []
    file_path = tmp_path / "voice.ogg"
    file_path.write_bytes(b"fake-audio")

    async def fake_post(self, url, *, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return {"status": "sent"}

    class FakeUploadResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "wamedia-audio-1"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, files=None):
            upload_calls.append({"url": url, "headers": headers, "files": files})
            return FakeUploadResponse()

    settings = get_settings()
    monkeypatch.setattr(settings, "url_wpp", "https://graph.facebook.com/v22.0/demo/messages")
    monkeypatch.setattr(settings, "token_whatsapp", "token-demo")
    monkeypatch.setattr(ChannelDeliveryService, "_post", fake_post)
    monkeypatch.setattr(
        "packages.channels.delivery.resolve_temp_media",
        lambda token: (file_path, {"mime_type": "audio/ogg", "filename": "voice.ogg"}),
    )
    monkeypatch.setattr("packages.channels.delivery.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        ChannelDeliveryService().deliver(
            channel="whatsapp",
            recipient="593999",
            message="https://bot.example/media/proxy-audio-token.ogg",
            media_type="audio",
        )
    )

    assert result["status"] == "sent"
    assert upload_calls[0]["url"] == "https://graph.facebook.com/v22.0/demo/media"
    assert upload_calls[0]["files"]["messaging_product"][1] == "whatsapp"
    assert upload_calls[0]["files"]["file"][2] == "audio/ogg"
    assert captured["payload"]["type"] == "audio"
    assert captured["payload"]["audio"]["id"] == "wamedia-audio-1"


def test_channel_delivery_transcodes_mp3_audio_before_whatsapp_upload(monkeypatch, tmp_path):
    captured = {}
    upload_calls = []
    file_path = tmp_path / "voice.mp3"
    file_path.write_bytes(b"fake-mp3")

    async def fake_post(self, url, *, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return {"status": "sent"}

    class FakeUploadResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "wamedia-audio-ogg-1"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, files=None):
            upload_calls.append({"url": url, "headers": headers, "files": files})
            return FakeUploadResponse()

    def fake_run(command, capture_output=True, check=False):
        output_path = command[-1]
        with open(output_path, "wb") as handle:
            handle.write(b"fake-ogg")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    settings = get_settings()
    monkeypatch.setattr(settings, "url_wpp", "https://graph.facebook.com/v22.0/demo/messages")
    monkeypatch.setattr(settings, "token_whatsapp", "token-demo")
    monkeypatch.setattr(ChannelDeliveryService, "_post", fake_post)
    monkeypatch.setattr(
        "packages.channels.delivery.resolve_temp_media",
        lambda token: (file_path, {"mime_type": "audio/mpeg", "filename": "voice.mp3"}),
    )
    monkeypatch.setattr("packages.channels.delivery.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("packages.channels.delivery.shutil.which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr("packages.channels.delivery.subprocess.run", fake_run)

    result = asyncio.run(
        ChannelDeliveryService().deliver(
            channel="whatsapp",
            recipient="593999",
            message="https://bot.example/media/proxy-audio-token.mp3",
            media_type="audio",
        )
    )

    assert result["status"] == "sent"
    assert upload_calls[0]["files"]["file"][0] == "voice.ogg"
    assert upload_calls[0]["files"]["file"][1] == b"fake-ogg"
    assert upload_calls[0]["files"]["file"][2] == "audio/ogg"
    assert captured["payload"]["audio"]["id"] == "wamedia-audio-ogg-1"


def test_channel_delivery_falls_back_to_link_when_whatsapp_media_upload_fails(monkeypatch):
    captured = {}

    async def fake_post(self, url, *, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return {"status": "sent"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            class FakeGetResponse:
                content = b"fake-image"
                headers = {"Content-Type": "image/png"}

                def raise_for_status(self):
                    return None

            return FakeGetResponse()

        async def post(self, url, headers=None, files=None):
            request = httpx.Request("POST", url)
            raise httpx.ConnectError("upload failed", request=request)

    settings = get_settings()
    monkeypatch.setattr(settings, "url_wpp", "https://graph.facebook.com/v22.0/demo/messages")
    monkeypatch.setattr(settings, "token_whatsapp", "token-demo")
    monkeypatch.setattr(ChannelDeliveryService, "_post", fake_post)
    monkeypatch.setattr("packages.channels.delivery.resolve_temp_media", lambda token: (None, None))
    monkeypatch.setattr("packages.channels.delivery.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        ChannelDeliveryService().deliver(
            channel="whatsapp",
            recipient="593999",
            message="https://bot.example/media/proxy-token.png",
            media_type="image",
        )
    )

    assert result["status"] == "sent"
    assert captured["payload"]["type"] == "image"
    assert captured["payload"]["image"]["link"] == "https://bot.example/media/proxy-token.png"
