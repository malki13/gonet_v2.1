"""Validaciones de seguridad para la API de entrada."""

import hashlib
import hmac

from fastapi import HTTPException, Request

from packages.shared.config import get_settings


def _extract_bearer_token(request: Request) -> str | None:
    """Extrae token de bearer."""
    authorization = str(request.headers.get("Authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        return token or None
    return None


def _extract_internal_secret(request: Request) -> str | None:
    """Extrae secreto de internal."""
    for header_name in ("X-Internal-Secret", "X-API-Key"):
        candidate = str(request.headers.get(header_name) or "").strip()
        if candidate:
            return candidate
    return _extract_bearer_token(request)


def validate_runtime_security() -> None:
    """Valida runtime security."""
    get_settings().validate_required_runtime_secrets()


def enforce_internal_auth(request: Request) -> None:
    """Devuelve el auth enforce internal."""
    settings = get_settings()
    expected_secret = str(settings.bot_api_internal_secret or "").strip()
    if not expected_secret:
        if settings.allow_insecure_local_bypass:
            return
        raise HTTPException(status_code=503, detail="internal_auth_not_configured")
    provided_secret = _extract_internal_secret(request)
    if provided_secret and hmac.compare_digest(provided_secret, expected_secret):
        return
    raise HTTPException(status_code=401, detail="invalid_internal_secret")


def enforce_meta_signature(request: Request, body: bytes) -> None:
    """Devuelve el signature enforce meta."""
    settings = get_settings()
    app_secret = str(settings.meta_app_secret or "").strip()
    if not app_secret:
        if settings.allow_insecure_local_bypass:
            return
        raise HTTPException(status_code=503, detail="meta_signature_not_configured")
    provided = str(request.headers.get("X-Hub-Signature-256") or "").strip()
    if not provided:
        raise HTTPException(status_code=401, detail="missing_meta_signature")
    expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if hmac.compare_digest(provided, expected):
        return
    raise HTTPException(status_code=401, detail="invalid_meta_signature")
