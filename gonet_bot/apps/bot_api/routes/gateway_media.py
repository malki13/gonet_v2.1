"""Utilidades del gateway para resolver y normalizar referencias de media."""

import logging
from pathlib import Path

import httpx

from packages.channels.media_proxy import (
    build_public_media_url,
    store_temp_media,
    store_temp_base64_media,
)
from packages.integrations.odoo_jsonrpc import OdooJsonRpcClient
from packages.shared.config import get_settings

logger = logging.getLogger("gateway.media")
GATEWAY_MEDIA_ERRORS = (httpx.HTTPError, RuntimeError, ValueError)


class _GatewayOdooJsonRpcClient(OdooJsonRpcClient):
    """Variacion del cliente JSON-RPC con overrides de conexion por fuente."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        request_log_tag: str,
        response_log_tag: str,
        timeout: float = 60.0,
        url_override: str | None = None,
        db_override: str | None = None,
        username_override: str | None = None,
        password_override: str | None = None,
        uid_override: int | None = None,
    ):
        """Inicializa el _gatewayodoojsonrpcclient con la configuracion necesaria."""
        super().__init__(
            logger=logger,
            request_log_tag=request_log_tag,
            response_log_tag=response_log_tag,
            timeout=timeout,
        )
        self._url_override = str(url_override or "").strip() or None
        self._db_override = str(db_override or "").strip() or None
        self._username_override = str(username_override or "").strip() or None
        self._password_override = str(password_override or "").strip() or None
        self._uid_override = uid_override if uid_override and uid_override > 0 else None

    def _url(self) -> str | None:
        """Devuelve la URL configurada para esta integracion."""
        return self._url_override or super()._url()

    def _db(self) -> str | None:
        """Devuelve la base de datos configurada para esta integracion."""
        return self._db_override or super()._db()

    def _username(self) -> str | None:
        """Devuelve el usuario configurado para esta integracion."""
        return self._username_override or super()._username()

    def _configured_uid(self) -> int | None:
        """Devuelve el UID fijo si esta configurado."""
        return self._uid_override or super()._configured_uid()

    def _password(self) -> str | None:
        """Devuelve la contraseña configurada para esta integracion."""
        return self._password_override or super()._password()


def _positive_int(value) -> int | None:
    """Devuelve el int positive."""
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _extract_base64_image_payload(payload: dict) -> tuple[str | None, str | None, str | None]:
    """Extrae el payload de imagen en base64."""
    if payload.get("base64_data"):
        return (
            str(payload.get("base64_data") or "").strip() or None,
            str(payload.get("mime_type") or "").strip() or None,
            str(payload.get("filename") or "").strip() or None,
        )
    attachments = payload.get("attachments")
    if isinstance(attachments, list):
        for item in attachments:
            if not isinstance(item, dict):
                continue
            base64_data = str(item.get("base64_data") or "").strip()
            if base64_data:
                return (
                    base64_data,
                    str(item.get("mime_type") or "").strip() or None,
                    str(item.get("filename") or "").strip() or None,
                )
    return None, None, None


def _payload_attachment_items(payload: dict) -> list[dict]:
    """Devuelve los adjuntos del payload."""
    attachments = payload.get("attachments")
    if not isinstance(attachments, list):
        return []
    return [item for item in attachments if isinstance(item, dict)]


def _extract_inline_attachment_payload(payload: dict) -> tuple[str | None, str | None, str | None, str | None]:
    """Extrae el payload inline del adjunto."""
    direct_url = str(payload.get("url") or "").strip() or None
    base64_data = str(payload.get("base64_data") or "").strip() or None
    mime_type = str(payload.get("mime_type") or "").strip() or None
    filename = str(payload.get("filename") or "").strip() or None
    if direct_url or base64_data:
        return direct_url, base64_data, mime_type, filename
    for item in _payload_attachment_items(payload):
        item_url = str(item.get("url") or item.get("link") or "").strip() or None
        item_base64 = str(item.get("base64_data") or "").strip() or None
        if not item_url and not item_base64:
            continue
        return (
            item_url,
            item_base64,
            str(item.get("mime_type") or "").strip() or None,
            str(item.get("filename") or "").strip() or None,
        )
    return None, None, None, None


def _infer_internal_media_type(
    *,
    tipo: str | None = None,
    mime_type: str | None = None,
    filename: str | None = None,
) -> str | None:
    """Infere el tipo de media interno a partir del tipo, mime o nombre."""
    normalized_tipo = str(tipo or "").strip().lower()
    if normalized_tipo == "imagen":
        return "image"
    if normalized_tipo == "audio":
        return "audio"
    if normalized_tipo == "documento":
        return "document"
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if normalized_mime.startswith("image/"):
        return "image"
    if normalized_mime.startswith("audio/"):
        return "audio"
    if normalized_mime:
        return "document"
    suffix = Path(str(filename or "").strip()).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return "image"
    if suffix in {".ogg", ".mp3", ".wav", ".m4a", ".aac"}:
        return "audio"
    if suffix:
        return "document"
    return None


def _attachment_ids_from_recordset(recordset) -> list[int]:
    """Devuelve el IDs de adjunto del recordset."""
    if isinstance(recordset, dict):
        recordset = [recordset]
    if not isinstance(recordset, list):
        return []
    found: list[int] = []
    for item in recordset:
        if not isinstance(item, dict):
            continue
        attachment_ids = item.get("attachment_ids") or []
        if not isinstance(attachment_ids, list):
            continue
        for raw_id in attachment_ids:
            attachment_id = _positive_int(raw_id)
            if attachment_id and attachment_id not in found:
                found.append(attachment_id)
    return found


def _payload_attachment_id_candidates(payload: dict) -> list[int]:
    """Recolecta candidatos de ids de adjunto desde el payload crudo."""
    found: list[int] = []

    def _add(value) -> None:
        """Agrega el elemento si todavia no existe."""
        attachment_id = _positive_int(value)
        if attachment_id and attachment_id not in found:
            found.append(attachment_id)

    for key in ("attachment_id", "attachmentId", "ir_attachment_id", "irAttachmentId"):
        _add(payload.get(key))
    attachment_ids = payload.get("attachment_ids")
    if isinstance(attachment_ids, list):
        for item in attachment_ids:
            _add(item)
    for item in _payload_attachment_items(payload):
        for key in ("attachment_id", "attachmentId", "ir_attachment_id", "irAttachmentId", "id"):
            _add(item.get(key))
        nested_ids = item.get("attachment_ids")
        if isinstance(nested_ids, list):
            for nested in nested_ids:
                _add(nested)
    return found


def _resolve_media_reference(payload: dict) -> str | None:
    """Resuelve reference media."""
    direct_ref = str(payload.get("message") or payload.get("mensaje") or payload.get("url") or "").strip()
    if direct_ref.startswith(("http://", "https://")):
        return direct_ref
    direct_url, inline_base64, inline_mime, inline_filename = _extract_inline_attachment_payload(payload)
    if direct_url and direct_url.startswith(("http://", "https://")):
        return direct_url
    if inline_base64:
        token = store_temp_base64_media(inline_base64, inline_mime, filename=inline_filename)
        if token:
            return build_public_media_url(token)
    base64_data, mime_type, filename = _extract_base64_image_payload(payload)
    if not base64_data:
        return direct_ref or None
    token = store_temp_base64_media(base64_data, mime_type, filename=filename)
    if not token:
        return direct_ref or None
    return build_public_media_url(token)


async def _download_whatsapp_media_reference(media_id: str, payload: dict) -> str | None:
    """Descarga media de WhatsApp y la publica temporalmente."""
    settings = get_settings()
    token = settings.token_whatsapp or settings.whatsapp_media_token
    if not media_id or not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    info_url = f"https://graph.facebook.com/{settings.whatsapp_graph_version}/{media_id}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            info_response = await client.get(info_url, headers=headers)
            info_response.raise_for_status()
            info_body = info_response.json()
            media_url = str(info_body.get("url") or "").strip()
            if not media_url:
                return None
            file_response = await client.get(media_url, headers=headers)
            file_response.raise_for_status()
            mime_type = str(payload.get("mime_type") or file_response.headers.get("Content-Type") or "").split(";")[0].strip() or None
            token_name = store_temp_media(
                file_response.content,
                mime_type,
                filename=str(payload.get("filename") or f"media_{media_id}").strip() or None,
            )
            if not token_name:
                return None
            return build_public_media_url(token_name)
    except GATEWAY_MEDIA_ERRORS:
        logger.exception("gateway_media_id_resolve_failed media_id=%s", media_id)
        return None


def _first_attachment_id(recordset) -> int | None:
    """Retorna el primer id de adjunto encontrado en un recordset."""
    found = _attachment_ids_from_recordset(recordset)
    return found[0] if found else None


def _iter_odoo_media_sources():
    """Itera fuentes potenciales de Odoo para resolver adjuntos."""
    settings = get_settings()
    candidates = [
        {
            "label": "jsonrpc_primary",
            "jsonrpc_url": settings.odoo_jsonrpc_url,
            "db": settings.odoo_jsonrpc_db,
            "username": settings.odoo_jsonrpc_username,
            "password": settings.odoo_jsonrpc_password,
            "web_url": settings.odoo_jsonrpc_web_url,
        },
    ]
    seen: set[tuple[str | None, str | None, str | None, str | None, str | None]] = set()
    for item in candidates:
        key = (
            str(item.get("jsonrpc_url") or "").strip() or None,
            str(item.get("db") or "").strip() or None,
            str(item.get("username") or "").strip() or None,
            str(item.get("password") or "").strip() or None,
            str(item.get("web_url") or "").strip() or None,
        )
        if key in seen:
            continue
        seen.add(key)
        if not all(key):
            continue
        yield item


async def _download_odoo_attachment_via_web(
    source: dict,
    attachment_id: int,
    *,
    filename: str | None,
    mime_type: str | None,
) -> str | None:
    """Descarga un adjunto de Odoo por la ruta web autenticada."""
    web_url = str(source.get("web_url") or "").strip()
    db = str(source.get("db") or "").strip()
    username = str(source.get("username") or "").strip()
    password = str(source.get("password") or "").strip()
    if not all([web_url, db, username, password, attachment_id]):
        return None
    try:
        async with httpx.AsyncClient(base_url=web_url.rstrip("/"), timeout=20.0, follow_redirects=True) as client:
            auth_response = await client.post(
                "/web/session/authenticate",
                json={
                    "jsonrpc": "2.0",
                    "params": {
                        "db": db,
                        "login": username,
                        "password": password,
                    },
                },
            )
            auth_response.raise_for_status()
            auth_body = auth_response.json()
            if not (auth_body.get("result") or {}).get("uid"):
                return None
            file_response = await client.get(f"/web/content/{attachment_id}")
            file_response.raise_for_status()
            token_name = store_temp_media(
                file_response.content,
                mime_type or str(file_response.headers.get("Content-Type") or "").split(";")[0].strip() or None,
                filename=filename,
            )
            if not token_name:
                return None
            return build_public_media_url(token_name)
    except GATEWAY_MEDIA_ERRORS:
        logger.exception("gateway_odoo_media_web_download_failed db=%s attachment_id=%s", db, attachment_id)
        return None


async def _resolve_odoo_attachment_media(payload: dict, *, allow_channel_fallback: bool = True) -> tuple[str | None, str | None]:
    """Resuelve media del adjunto de Odoo."""
    direct_url, inline_base64, inline_mime, inline_filename = _extract_inline_attachment_payload(payload)
    inline_type = _infer_internal_media_type(
        tipo=payload.get("tipo"),
        mime_type=inline_mime,
        filename=inline_filename,
    )
    if inline_type:
        if direct_url and direct_url.startswith(("http://", "https://")):
            return inline_type, direct_url
        if inline_base64:
            token = store_temp_base64_media(inline_base64, inline_mime, filename=inline_filename)
            if token:
                return inline_type, build_public_media_url(token)

    message_id = _positive_int(payload.get("message_id") or payload.get("messageId"))
    channel_id = _positive_int(payload.get("channel_id") or payload.get("channelId")) if allow_channel_fallback else None
    attachment_ids = _payload_attachment_id_candidates(payload)
    if not attachment_ids and not message_id and not channel_id:
        return None, None
    for source in _iter_odoo_media_sources():
        client = _GatewayOdooJsonRpcClient(
            logger=logger,
            request_log_tag="gateway_odoo_media_jsonrpc_request",
            response_log_tag="gateway_odoo_media_jsonrpc",
            timeout=20.0,
            url_override=source.get("jsonrpc_url"),
            db_override=source.get("db"),
            username_override=source.get("username"),
            password_override=source.get("password"),
        )
        if not client.is_configured():
            continue
        try:
            resolved_attachment_ids = list(attachment_ids)
            if message_id:
                rows = await client.execute_kw(
                    "mail.message",
                    "read",
                    args=[[message_id]],
                    kwargs={"fields": ["attachment_ids"]},
                )
                for attachment_id in _attachment_ids_from_recordset(rows):
                    if attachment_id not in resolved_attachment_ids:
                        resolved_attachment_ids.append(attachment_id)
            if not resolved_attachment_ids and channel_id:
                rows = await client.execute_kw(
                    "mail.message",
                    "search_read",
                    args=[[["model", "=", "mail.channel"], ["res_id", "=", channel_id], ["attachment_ids", "!=", False]]],
                    kwargs={"fields": ["id", "attachment_ids"], "order": "id desc", "limit": 3},
                )
                for attachment_id in _attachment_ids_from_recordset(rows):
                    if attachment_id not in resolved_attachment_ids:
                        resolved_attachment_ids.append(attachment_id)
            if not resolved_attachment_ids:
                continue
            for attachment_id in resolved_attachment_ids:
                rows = await client.execute_kw(
                    "ir.attachment",
                    "read",
                    args=[[attachment_id]],
                    kwargs={"fields": ["mimetype", "name"]},
                )
                if isinstance(rows, dict):
                    rows = [rows]
                if not isinstance(rows, list) or not rows:
                    continue
                attachment = rows[0] if isinstance(rows[0], dict) else {}
                filename = str(attachment.get("name") or payload.get("filename") or f"attachment_{attachment_id}").strip() or None
                mime_type = str(attachment.get("mimetype") or "").strip() or None
                downloaded = await _download_odoo_attachment_via_web(
                    source,
                    attachment_id,
                    filename=filename,
                    mime_type=mime_type,
                )
                if downloaded:
                    media_type = _infer_internal_media_type(
                        tipo=payload.get("tipo"),
                        mime_type=mime_type,
                        filename=filename,
                    )
                    return media_type, downloaded
        except GATEWAY_MEDIA_ERRORS:
            logger.exception(
                "gateway_odoo_media_resolve_failed db=%s message_id=%s channel_id=%s",
                source.get("db"),
                message_id,
                channel_id,
            )
    return None, None


async def _download_odoo_attachment_reference(payload: dict) -> str | None:
    """Atajo que devuelve solo la referencia de media de Odoo."""
    _, reference = await _resolve_odoo_attachment_media(payload, allow_channel_fallback=True)
    return reference


def _map_internal_media_type(tipo: str | None) -> str | None:
    """Mapea el tipo interno de media al contrato del canal."""
    normalized = str(tipo or "").strip().lower()
    if normalized == "imagen":
        return "image"
    if normalized == "audio":
        return "audio"
    if normalized == "documento":
        return "document"
    return None


def _map_inbound_attachment_tipo(raw_type: str | None) -> str | None:
    """Mapea el tipo bruto del adjunto entrante al tipo de Odoo Chat."""
    normalized = str(raw_type or "").strip().lower()
    if normalized == "image":
        return "imagen"
    if normalized == "audio":
        return "audio"
    if normalized in {"document", "file"}:
        return "documento"
    return None


def _resolve_inbound_attachment_reference(attachment) -> str | None:
    """Resuelve referencia del adjunto entrante."""
    base64_data = str(getattr(attachment, "base64_data", "") or "").strip()
    mime_type = str(getattr(attachment, "mime_type", "") or "").strip() or None
    filename = str(getattr(attachment, "filename", "") or "").strip() or None
    if base64_data:
        token = store_temp_base64_media(base64_data, mime_type, filename=filename)
        if token:
            return build_public_media_url(token)
    direct_ref = str(getattr(attachment, "url", "") or "").strip()
    if direct_ref.startswith(("http://", "https://")):
        return direct_ref
    return None
