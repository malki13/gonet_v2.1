import base64
import json

from packages.channels import media_proxy


def test_store_temp_base64_media_uses_mime_extension_when_filename_has_no_suffix(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_PROXY_DIR", str(tmp_path))

    token = media_proxy.store_temp_base64_media(
        base64.b64encode(b"fake-image").decode("ascii"),
        "image/jpeg",
        filename="image_1186454436736334",
    )

    assert token is not None
    assert token.endswith(".jpg")

    file_path, meta = media_proxy.resolve_temp_media(token)

    assert file_path is not None
    assert file_path.name.endswith(".jpg")
    assert meta["mime_type"] == "image/jpeg"
    assert meta["filename"] == "image_1186454436736334.jpg"


def test_store_and_resolve_temp_media_uses_redis_fallback_when_local_file_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_PROXY_DIR", str(tmp_path))
    monkeypatch.setattr(media_proxy.get_settings(), "redis_url", "redis://fake/0")

    class FakeRedis:
        def __init__(self):
            self.store = {}

        def ping(self):
            return True

        def set(self, key, value, ex=None):
            self.store[key] = value

        def get(self, key):
            return self.store.get(key)

    fake_redis = FakeRedis()
    monkeypatch.setattr(media_proxy, "_redis_client", fake_redis)

    token = media_proxy.store_temp_base64_media(
        base64.b64encode(b"fake-image").decode("ascii"),
        "image/jpeg",
        filename="image_1186454436736334",
    )

    assert token is not None
    file_path = tmp_path / token
    meta_path = file_path.with_suffix(f"{file_path.suffix}.json")
    file_path.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)

    resolved_path, meta = media_proxy.resolve_temp_media(token)

    assert resolved_path is not None
    assert resolved_path.exists()
    assert resolved_path.name.endswith(".jpg")
    assert meta["mime_type"] == "image/jpeg"
    assert meta["filename"] == "image_1186454436736334.jpg"
    assert json.loads(fake_redis.get(f"media:proxy:{token}"))["filename"] == "image_1186454436736334.jpg"
