import pytest

from apps.bot_api.dependencies import get_orchestrator
from packages.integrations.contact_registry import build_contact_registry
from packages.integrations.ocr_callback_store import build_ocr_callback_store
from packages.integrations.redis_store import build_session_store
from packages.shared.config import get_settings


@pytest.fixture(autouse=True)
def isolated_test_settings(monkeypatch):
    settings = get_settings()
    build_session_store.cache_clear()
    build_contact_registry.cache_clear()
    build_ocr_callback_store.cache_clear()
    get_orchestrator.cache_clear()
    monkeypatch.setattr(settings, "app_env", "local")
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "contact_pg_dsn", None)
    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "ocr_async_enabled", False)
    monkeypatch.setattr(settings, "url_odoo_chat", None)
    monkeypatch.setattr(settings, "enable_inactivity_scheduler", False)
    monkeypatch.setattr(settings, "onu_base_url", None)
    monkeypatch.setattr(settings, "public_base_url", None)
    monkeypatch.setattr(settings, "bot_api_internal_secret", None)
    monkeypatch.setattr(settings, "meta_app_secret", None)
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "conversation_classifier_mode", "heuristic")
    yield
    build_session_store.cache_clear()
    build_contact_registry.cache_clear()
    build_ocr_callback_store.cache_clear()
    get_orchestrator.cache_clear()
