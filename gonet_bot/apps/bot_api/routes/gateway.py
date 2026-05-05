"""gateway principal de Meta e internos."""

import asyncio
import logging
from json import JSONDecodeError

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import PlainTextResponse, Response

from apps.bot_api.dependencies import get_orchestrator
from apps.bot_api.routes.gateway_dispatch import (
    coalesce_and_process,
    dispatch_internal_send,
    prime_sales_capture_session,
    process_and_deliver,
    relay_human_message,
    resolve_runtime_session,
    should_relay_human_message,
)
from apps.bot_api.routes.gateway_media import (
    _download_odoo_attachment_reference,
    _resolve_odoo_attachment_media,
    _download_whatsapp_media_reference,
    _map_inbound_attachment_tipo,
    _map_internal_media_type,
    _resolve_inbound_attachment_reference,
    _resolve_media_reference,
)
from apps.bot_api.security import enforce_internal_auth, enforce_meta_signature
from packages.channels.delivery import ChannelDeliveryService
from packages.channels.media_proxy import (
    register_runtime_base_url,
    resolve_temp_media,
)
from packages.channels.meta_inbound import extract_meta_verification, normalize_meta_event
from packages.integrations.contact_registry import build_contact_registry
from packages.integrations.odoo_chat import OdooChatClient
from packages.orchestrator.service import OrchestratorService
from packages.orchestrator.session_context import SessionContextService
from packages.shared.config import get_settings

router = APIRouter(tags=["gateway"])
logger = logging.getLogger("gateway")
_runtime_tasks: set[asyncio.Task] = set()


def _preview(text: str | None, *, limit: int = 160) -> str:
    """Recorta y compacta el texto para dejarlo legible en logs."""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}..."

async def _request_payload(request: Request) -> dict | None:
    """Intenta leer el request como JSON y, si falla, como formulario."""
    try:
        data = await request.json()
        if isinstance(data, dict):
            return data
    except (JSONDecodeError, RuntimeError, ValueError):
        pass
    try:
        form = await request.form()
        if form:
            return dict(form)
    except (AssertionError, RuntimeError, ValueError):
        pass
    return None


async def _process_and_deliver(message, orchestrator: OrchestratorService) -> None:
    """Encola el mensaje para procesamiento completo y entrega al canal."""
    await process_and_deliver(
        message,
        orchestrator,
        delivery=ChannelDeliveryService(),
        odoo_chat=OdooChatClient(),
        logger_=logger,
        preview_fn=_preview,
    )


async def _coalesce_and_process(message, orchestrator: OrchestratorService) -> None:
    """Agrupa mensajes cortos antes de procesarlos y entregarlos al orquestador."""
    await coalesce_and_process(
        message,
        orchestrator,
        sessions=SessionContextService(),
        delivery=ChannelDeliveryService(),
        odoo_chat=OdooChatClient(),
        logger_=logger,
        preview_fn=_preview,
    )


def _schedule_runtime_task(coro) -> None:
    """Programa una tarea de runtime y registra fallos no observados."""
    task = asyncio.create_task(coro)
    _runtime_tasks.add(task)

    def _cleanup(done: asyncio.Task) -> None:
        """Limpia el estado temporal y captura errores no observados."""
        _runtime_tasks.discard(done)
        try:
            done.result()
        except Exception:
            logger.exception("gateway_runtime_task_failed")

    task.add_done_callback(_cleanup)


async def _resolve_runtime_session(message):
    """Resuelve session runtime."""
    return await resolve_runtime_session(
        message,
        registry=build_contact_registry(),
        sessions=SessionContextService(),
        logger_=logger,
    )


def _current_request_base_url(request: Request) -> str:
    """Devuelve el URL base publica de la solicitud."""
    proto = request.headers.get("X-Forwarded-Proto") or request.url.scheme or "http"
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.url.netloc
    return f"{proto}://{host}"


async def _resolve_internal_media_reference(payload: dict, *, channel: str, media_type: str | None) -> str | None:
    """Resuelve referencia interna de media."""
    resolved = _resolve_media_reference(payload)
    if str(resolved or "").strip().startswith(("http://", "https://")):
        return resolved
    if media_type in {"image", "audio", "document"}:
        odoo_media = await _download_odoo_attachment_reference(payload)
        if odoo_media:
            return odoo_media
    if channel == "whatsapp" and media_type in {"image", "audio", "document"}:
        media_id = str(payload.get("message") or payload.get("mensaje") or payload.get("url") or "").strip()
        downloaded = await _download_whatsapp_media_reference(media_id, payload)
        if downloaded:
            return downloaded
    if media_type in {"image", "audio", "document"}:
        return None
    return resolved


async def _resolve_internal_attachment_media(payload: dict, *, channel: str) -> tuple[str | None, str | None]:
    """Resuelve media del adjunto interno."""
    del channel
    return await _resolve_odoo_attachment_media(payload, allow_channel_fallback=True)


async def _should_relay_human_message(message) -> bool:
    """Determina si un inbound debe pasar por el flujo humano."""
    return await should_relay_human_message(
        message,
        registry=build_contact_registry(),
        sessions=SessionContextService(),
    )


async def _relay_human_message(message) -> None:
    """Reenvia el mensaje al canal humano de Odoo Chat."""
    await relay_human_message(
        message,
        contact_registry=build_contact_registry(),
        sessions=SessionContextService(),
        odoo_chat=OdooChatClient(),
        logger_=logger,
        map_inbound_attachment_tipo=_map_inbound_attachment_tipo,
        resolve_inbound_attachment_reference=_resolve_inbound_attachment_reference,
    )


async def _prime_sales_capture_session(payload: dict) -> None:
    """Prepara la sesion comercial antes de disparar un mensaje interno."""
    await prime_sales_capture_session(payload, store=SessionContextService())


async def _dispatch_internal_send(payload: dict) -> None:
    """Despacha un payload interno hacia el canal adecuado."""
    await dispatch_internal_send(
        payload,
        delivery=ChannelDeliveryService(),
        odoo_chat=OdooChatClient(),
        contact_registry=build_contact_registry(),
        sessions=SessionContextService(),
        logger_=logger,
        preview_fn=_preview,
        prime_sales_capture_session_cb=_prime_sales_capture_session,
        map_internal_media_type=_map_internal_media_type,
        resolve_internal_media_reference_cb=_resolve_internal_media_reference,
        resolve_internal_attachment_media_cb=_resolve_internal_attachment_media,
    )


@router.get("/")
async def verify_meta_webhook(request: Request):
    """Responde al challenge de verificacion de Meta."""
    settings = get_settings()
    mode, verify_token, challenge = extract_meta_verification(dict(request.query_params))
    if mode == "subscribe" and verify_token and verify_token == (settings.verify_token or ""):
        logger.info("meta_webhook_verified mode=%s challenge_len=%s", mode, len(challenge or ""))
        return PlainTextResponse(challenge or "", status_code=200)
    logger.warning("meta_webhook_verification_failed mode=%s", mode)
    return PlainTextResponse("Error de verificación", status_code=403)


@router.post("/")
async def handle_root_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    orchestrator: OrchestratorService = Depends(get_orchestrator),
):
    """Maneja root webhook y avanza el flujo."""
    register_runtime_base_url(_current_request_base_url(request))
    body = await request.body()
    data = await _request_payload(request)
    if not isinstance(data, dict):
        logger.info("meta_webhook_ignored reason=non_dict_payload")
        return PlainTextResponse("EVENT_RECEIVED", status_code=200)
    logger.info(
        "meta_webhook_received object=%s keys=%s",
        str(data.get("object") or "").strip() or "internal_or_unknown",
        sorted(data.keys()),
    )

    channel = data.get("chanel") or data.get("channel")
    if channel:
        enforce_internal_auth(request)
        background_tasks.add_task(_dispatch_internal_send, data)
        logger.info("meta_webhook_internal_dispatch channel=%s recipient=%s", channel, data.get("recipient"))
        return PlainTextResponse("Enviado", status_code=200)

    enforce_meta_signature(request, body)
    messages = await normalize_meta_event(data)
    if not messages:
        logger.info(
            "meta_webhook_ignored reason=no_inbound_messages object=%s keys=%s",
            str(data.get("object") or "").strip() or "unknown",
            sorted(data.keys()),
        )
        return PlainTextResponse("EVENT_RECEIVED", status_code=200)

    logger.info("meta_webhook_normalized messages=%s", len(messages))
    for message in messages:
        message = await _resolve_runtime_session(message)
        logger.info(
            "meta_webhook_message session_id=%s channel=%s recipient=%s message_type=%s attachments=%s location=%s preview=%r",
            message.session_id,
            message.channel,
            message.recipient,
            (message.metadata or {}).get("message_type"),
            len(message.attachments or []),
            bool(message.location),
            _preview(message.mensaje),
        )
        if await _should_relay_human_message(message):
            logger.info(
                "gateway_human_relay_queued session_id=%s channel=%s recipient=%s attachments=%s location=%s",
                message.session_id,
                message.channel,
                message.recipient,
                len(message.attachments or []),
                bool(message.location),
            )
            background_tasks.add_task(_relay_human_message, message)
            continue
        _schedule_runtime_task(_coalesce_and_process(message, orchestrator))
    return PlainTextResponse("EVENT_RECEIVED", status_code=200)


@router.post("/send")
async def handle_send(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Envía un mensaje interno directo al canal configurado."""
    enforce_internal_auth(request)
    register_runtime_base_url(_current_request_base_url(request))
    data = await _request_payload(request)
    if not isinstance(data, dict):
        return PlainTextResponse("Invalid payload", status_code=400)
    if not (data.get("chanel") or data.get("channel")):
        return PlainTextResponse("Invalid payload", status_code=400)
    logger.info(
        "send_endpoint_received channel=%s recipient=%s odoo_action=%s preview=%r",
        data.get("chanel") or data.get("channel"),
        data.get("recipient"),
        data.get("odoo"),
        _preview(str(data.get("message") or data.get("mensaje") or "")),
    )
    background_tasks.add_task(_dispatch_internal_send, data)
    return PlainTextResponse("Enviado", status_code=200)


@router.get("/media/{token}")
async def get_media_proxy(token: str):
    """Sirve el archivo temporal asociado al token de media."""
    file_path, meta = resolve_temp_media(token)
    if not file_path or not meta:
        return PlainTextResponse("Not found", status_code=404)
    return Response(
        file_path.read_bytes(),
        headers={"Content-Disposition": f'inline; filename="{meta.get("filename", token)}"'},
        media_type=meta.get("mime_type") or "application/octet-stream",
    )


@router.get("/send/media/{token}")
async def get_legacy_send_media_proxy(token: str):
    """Sirve compatibilidad para referencias antiguas de media bajo `/send/media`."""
    return await get_media_proxy(token)


@router.post("/close")
async def close_session(
    request: Request,
    orchestrator: OrchestratorService = Depends(get_orchestrator),
):
    """Cierra la sesion operativa y sincroniza con el registro de contactos."""
    enforce_internal_auth(request)
    data = await _request_payload(request)
    if not isinstance(data, dict):
        return PlainTextResponse("Invalid payload", status_code=400)
    session_id = str(data.get("session_id") or "").strip() or None
    recipient = str(data.get("recipient") or "").strip() or None
    channel = str(data.get("chanel") or data.get("channel") or "").strip().lower() or None
    registry = build_contact_registry()
    registry_result = {"status": "skipped"}
    channel_id = str(data.get("channel_id") or "").strip()
    if channel_id:
        registry_result = await registry.close_by_channel_id(channel_id)
    elif recipient and channel:
        registry_result = await registry.close_contact(recipient=recipient, red=channel)
    target_recipient = str(registry_result.get("recipient") or recipient or "").strip() or None
    closed = await orchestrator.sessions.clear(session_id=session_id, recipient=target_recipient, channel=channel)
    logger.info(
        "close_session session_id=%s recipient=%s channel=%s closed=%s registry_status=%s",
        session_id,
        target_recipient,
        channel,
        bool(closed),
        registry_result.get("status"),
    )
    return {"status": "ok", "closed": bool(closed or registry_result.get("status") == "closed")}
