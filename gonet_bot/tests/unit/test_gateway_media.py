from apps.bot_api.routes.gateway_media import _iter_odoo_media_sources
from packages.shared.config import get_settings


def test_iter_odoo_media_sources_uses_only_jsonrpc_database(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "odoo_jsonrpc_url", "https://erp-app.gonet.ec/jsonrpc")
    monkeypatch.setattr(settings, "odoo_jsonrpc_db", "app")
    monkeypatch.setattr(settings, "odoo_jsonrpc_username", "gonet")
    monkeypatch.setattr(settings, "odoo_jsonrpc_password", "secret")
    monkeypatch.setattr(settings, "odoo_jsonrpc_web_url", "https://erp-app.gonet.ec")
    monkeypatch.setattr(settings, "odoo_db", "telecomgroup")
    monkeypatch.setattr(settings, "odoo_username", "info.tics@gonet.ec")
    monkeypatch.setattr(settings, "odoo_password", "other-secret")
    monkeypatch.setattr(settings, "odoo_url", "http://172.25.1.21")

    sources = list(_iter_odoo_media_sources())

    assert len(sources) == 1
    assert sources[0]["db"] == "app"
    assert sources[0]["username"] == "gonet"
    assert sources[0]["web_url"] == "https://erp-app.gonet.ec"


def test_iter_odoo_media_sources_skips_when_web_url_missing(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "odoo_jsonrpc_url", "https://erp-app.gonet.ec/jsonrpc")
    monkeypatch.setattr(settings, "odoo_jsonrpc_db", "app")
    monkeypatch.setattr(settings, "odoo_jsonrpc_username", "gonet")
    monkeypatch.setattr(settings, "odoo_jsonrpc_password", "secret")
    monkeypatch.setattr(settings, "odoo_jsonrpc_web_url", None)
    monkeypatch.setattr(settings, "odoo_db", "telecomgroup")
    monkeypatch.setattr(settings, "odoo_username", "info.tics@gonet.ec")
    monkeypatch.setattr(settings, "odoo_password", "other-secret")
    monkeypatch.setattr(settings, "odoo_url", "http://172.25.1.21")

    sources = list(_iter_odoo_media_sources())

    assert sources == []
