"""Proxy de media temporal para adjuntos y respuestas intermedias."""

import base64
import json
import ipaddress
import logging
import mimetypes
import os
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from packages.shared.config import get_settings

logger = logging.getLogger("channels.media_proxy")

_DEFAULT_BASE_URL = "http://localhost:8010"
_DEFAULT_TTL_SECONDS = 3600
_DEFAULT_DIRNAME = "gonet_platform_media"
_REDIS_KEY_PREFIX = "media:proxy:"
_runtime_base_url: str | None = None
_redis_client = None

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None


def _media_dir() -> Path:
    """Devuelve el dir media."""
    custom_dir = os.getenv("MEDIA_PROXY_DIR")
    base_dir = Path(custom_dir) if custom_dir else Path(tempfile.gettempdir()) / _DEFAULT_DIRNAME
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


def _metadata_path(file_path: Path) -> Path:
    """Devuelve el path metadata."""
    return file_path.with_suffix(f"{file_path.suffix}.json")


def _guess_extension(mime_type: str | None) -> str:
    """Devuelve la extension guess."""
    ext = mimetypes.guess_extension(str(mime_type or "").split(";")[0].strip().lower())
    if ext == ".jpe":
        return ".jpg"
    return ext or ".bin"


def _cleanup_expired_files() -> None:
    """Limpia files expired."""
    ttl_seconds = int(os.getenv("MEDIA_PROXY_TTL_SECONDS", str(_DEFAULT_TTL_SECONDS)))
    now = time.time()
    for file_path in _media_dir().iterdir():
        try:
            if not file_path.is_file():
                continue
            if now - file_path.stat().st_mtime <= ttl_seconds:
                continue
            file_path.unlink(missing_ok=True)
            if file_path.suffix != ".json":
                _metadata_path(file_path).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("media proxy cleanup failed path=%s error=%s", file_path, exc)


def _media_ttl_seconds() -> int:
    """Devuelve el TTL de media."""
    try:
        return max(60, int(os.getenv("MEDIA_PROXY_TTL_SECONDS", str(_DEFAULT_TTL_SECONDS))))
    except Exception:
        return _DEFAULT_TTL_SECONDS


def _redis_key(token: str) -> str:
    """Devuelve el clave redis."""
    return f"{_REDIS_KEY_PREFIX}{token}"


def _redis_media_client():
    """Devuelve el cliente redis para media."""
    global _redis_client
    settings = get_settings()
    if not settings.redis_url or redis is None:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        _redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except Exception:
        logger.exception("media proxy redis unavailable url=%s", settings.redis_url)
        _redis_client = None
        return None


def _store_media_record_redis(token: str, media_bytes: bytes, mime_type: str | None, filename: str | None) -> bool:
    """Guarda un media record en redis."""
    client = _redis_media_client()
    if client is None:
        return False
    ttl = _media_ttl_seconds()
    record = {
        "base64_data": base64.b64encode(media_bytes).decode("ascii"),
        "mime_type": str(mime_type or "application/octet-stream").split(";")[0].strip().lower(),
        "filename": str(filename or "").strip() or token,
        "created_at": int(time.time()),
    }
    try:
        client.set(_redis_key(token), json.dumps(record, ensure_ascii=False), ex=ttl)
        return True
    except Exception:
        logger.exception("media proxy redis store failed token=%s", token)
        return False


def _load_media_record_redis(token: str) -> dict | None:
    """Carga un media record desde redis."""
    client = _redis_media_client()
    if client is None:
        return None
    try:
        raw = client.get(_redis_key(token))
    except Exception:
        logger.exception("media proxy redis load failed token=%s", token)
        return None
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except Exception:
        logger.exception("media proxy redis record decode failed token=%s", token)
        return None
    return record if isinstance(record, dict) else None


def _is_public_base_url(base_url: str | None) -> bool:
    """Indica si URL public base se cumple."""
    clean = str(base_url or "").strip()
    if not clean:
        return False
    try:
        parsed = urlparse(clean)
    except Exception:
        return False
    hostname = str(parsed.hostname or "").strip().lower()
    if not hostname:
        return False
    if hostname in {"localhost", "host.docker.internal"} or hostname.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_reserved
        or ip.is_multicast
    )


def _normalize_media_base_url(base_url: str | None) -> str:
    """Reduce el base URL a su origen publico sin path extra."""
    clean = str(base_url or "").strip().rstrip("/")
    if not clean:
        return ""
    try:
        parsed = urlparse(clean)
    except Exception:
        return clean
    if not parsed.scheme or not parsed.netloc:
        return clean
    return f"{parsed.scheme}://{parsed.netloc}"


def build_public_media_url(token: str) -> str:
    """Construye URL public media a partir del contexto disponible."""
    settings = get_settings()
    configured_base_url = _normalize_media_base_url(settings.public_base_url)
    runtime_base_url = _normalize_media_base_url(_runtime_base_url)
    base_url = configured_base_url or runtime_base_url or _DEFAULT_BASE_URL
    return f"{base_url}/media/{token}"


def register_runtime_base_url(base_url: str | None) -> None:
    """Devuelve el URL register runtime base."""
    global _runtime_base_url
    clean = str(base_url or "").strip().rstrip("/")
    if not clean:
        return
    if _is_public_base_url(clean):
        _runtime_base_url = clean
        return
    if _runtime_base_url and _is_public_base_url(_runtime_base_url):
        logger.info(
            "media proxy ignored non-public runtime base url=%s keeping=%s",
            clean,
            _runtime_base_url,
        )
        return
    _runtime_base_url = clean


def store_temp_media(media_bytes: bytes, mime_type: str | None, filename: str | None = None) -> str | None:
    """Almacena media de temp."""
    if not media_bytes:
        return None
    _cleanup_expired_files()
    content_type = str(mime_type or "application/octet-stream").split(";")[0].strip().lower()
    provided_filename = os.path.basename(filename) if filename else ""
    provided_ext = Path(provided_filename).suffix
    ext = provided_ext or _guess_extension(content_type)
    stored_filename = provided_filename or ""
    if not stored_filename:
        stored_filename = f"media{ext or '.bin'}"
    elif not provided_ext and ext:
        stored_filename = f"{stored_filename}{ext}"
    token = f"{uuid.uuid4().hex}{ext or '.bin'}"
    file_path = _media_dir() / token
    meta = {
        "mime_type": content_type or "application/octet-stream",
        "filename": stored_filename or token,
        "created_at": int(time.time()),
    }
    stored = False
    try:
        file_path.write_bytes(media_bytes)
        _metadata_path(file_path).write_text(json.dumps(meta), encoding="utf-8")
        stored = True
    except OSError as exc:
        logger.warning("media proxy file store failed token=%s error=%s", token, exc)
    stored = _store_media_record_redis(token, media_bytes, content_type, stored_filename) or stored
    return token if stored else None


def store_temp_base64_media(base64_data: str | None, mime_type: str | None, filename: str | None = None) -> str | None:
    """Almacena media de temp base64."""
    raw = str(base64_data or "").strip()
    if not raw:
        return None
    if raw.startswith("data:") and "," in raw:
        header, raw = raw.split(",", 1)
        if not mime_type and ";" in header:
            mime_type = header.split(":", 1)[1].split(";", 1)[0].strip()
    try:
        media_bytes = base64.b64decode(raw, validate=False)
    except Exception:
        logger.exception("media proxy base64 decode failed")
        return None
    return store_temp_media(media_bytes, mime_type, filename=filename)


def resolve_temp_media(token: str):
    """Resuelve media temp."""
    safe_token = os.path.basename(str(token or ""))
    if not safe_token:
        return None, None
    file_path = _media_dir() / safe_token
    meta_path = _metadata_path(file_path)
    if file_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("media proxy metadata read failed token=%s", safe_token)
            return None, None
        return file_path, meta
    record = _load_media_record_redis(safe_token)
    if not record:
        return None, None
    try:
        media_bytes = base64.b64decode(str(record.get("base64_data") or "").encode("ascii"), validate=False)
    except Exception:
        logger.exception("media proxy redis payload decode failed token=%s", safe_token)
        return None, None
    meta = {
        "mime_type": str(record.get("mime_type") or "application/octet-stream").split(";")[0].strip().lower(),
        "filename": str(record.get("filename") or safe_token).strip() or safe_token,
    }
    try:
        file_path.write_bytes(media_bytes)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    except OSError:
        logger.exception("media proxy local cache write failed token=%s", safe_token)
    return file_path, meta
