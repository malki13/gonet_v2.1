import pytest
from fastapi.testclient import TestClient

from apps.bot_api.main import app
from apps.bot_api.security import validate_runtime_security
from packages.shared.config import get_settings


@pytest.fixture(autouse=True)
def isolated_security_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "local")
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(settings, "enable_inactivity_scheduler", False)
    monkeypatch.setattr(settings, "bot_api_internal_secret", None)
    monkeypatch.setattr(settings, "meta_app_secret", None)


def test_runtime_security_allows_local_bypass():
    validate_runtime_security()


def test_runtime_security_requires_secrets_outside_local(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "production")

    with pytest.raises(RuntimeError, match="missing_required_runtime_secrets:BOT_API_INTERNAL_SECRET,META_APP_SECRET"):
        validate_runtime_security()


def test_app_startup_fails_without_required_secrets_outside_local(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "production")

    with pytest.raises(RuntimeError, match="missing_required_runtime_secrets:BOT_API_INTERNAL_SECRET,META_APP_SECRET"):
        with TestClient(app):
            pass


def test_media_proxy_prefers_public_base_url_setting(monkeypatch):
    from packages.channels import media_proxy

    settings = get_settings()
    monkeypatch.setattr(settings, "public_base_url", "https://public.example")
    media_proxy._runtime_base_url = None
    media_proxy.register_runtime_base_url("http://host.docker.internal:8010")

    assert media_proxy.build_public_media_url("archivo.png") == "https://public.example/media/archivo.png"

    media_proxy._runtime_base_url = None


def test_media_proxy_strips_path_from_public_base_url(monkeypatch):
    from packages.channels import media_proxy

    settings = get_settings()
    monkeypatch.setattr(settings, "public_base_url", "https://public.example/send")
    media_proxy._runtime_base_url = None

    assert media_proxy.build_public_media_url("archivo.png") == "https://public.example/media/archivo.png"
