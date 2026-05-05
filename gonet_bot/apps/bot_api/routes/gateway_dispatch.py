"""Funciones de despacho que conectan el gateway con el orquestador."""

import asyncio
import uuid

import httpx

from packages.channels.media_proxy import build_public_media_url, store_temp_media
from packages.shared.config import get_settings
from packages.integrations.text_to_speech import TextToSpeechService
from packages.shared.schemas import Attachment, InboundMessage, Location, SessionState

_coalescing_locks: dict[str, asyncio.Lock] = {}


def _build_text_to_speech_service():
    """Construye servicio de texto a voz a partir del contexto disponible."""
    return TextToSpeechService()


def _message_ids(message) -> list[str]:
    """Extrae y desduplica los IDs asociados al mensaje."""
    metadata = getattr(message, "metadata", {}) or {}
    ids = []
    primary = str(metadata.get("message_id") or "").strip()
    if primary:
        ids.append(primary)
    for item in metadata.get("coalesced_message_ids") or []:
        normalized = str(item or "").strip()
        if normalized and normalized not in ids:
            ids.append(normalized)
    return ids


def _coalescing_key(message) -> str:
    """Genera la clave que agrupa mensajes por canal, destinatario y sesion."""
    return f"{message.channel}:{message.recipient}:{message.session_id}"


def _coalescing_lock(message) -> asyncio.Lock:
    """Devuelve el candado compartido que serializa la coalescencia."""
    key = _coalescing_key(message)
    lock = _coalescing_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _coalescing_locks[key] = lock
    return lock


def _looks_like_human_handoff_state(state: SessionState | None) -> bool:
    """Indica si el estado ya quedó marcado como handoff humano."""
    if state is None:
        return False
    if bool(state.human_handoff):
        return True
    current_intent = str(getattr(state, "current_intent", "") or "").strip().lower()
    last_agent = str(getattr(state, "last_agent", "") or "").strip().lower()
    if current_intent == "human_handoff" or last_agent == "handoff":
        return True
    metadata = getattr(state, "metadata", {}) or {}
    for key in ("handoff_summary", "handoff_group", "handoff_origen"):
        if str(metadata.get(key) or "").strip():
            return True
    return False


def _should_coalesce_message(message) -> bool:
    """Indica si mensaje coalesce se cumple."""
    settings = get_settings()
    if not settings.inbound_coalescing_enabled:
        return False
    if getattr(message, "location", None):
        return False
    attachments = getattr(message, "attachments", None) or []
    if attachments:
        coalescible_attachment = False
        for item in attachments:
            attachment_type = str(getattr(item, "type", None) or "").strip().lower()
            mime_type = str(getattr(item, "mime_type", None) or "").split(";", 1)[0].strip().lower()
            if attachment_type in {"image", "document", "file"}:
                coalescible_attachment = True
                break
            if mime_type.startswith("image/") or mime_type.startswith("application/"):
                coalescible_attachment = True
                break
        if not coalescible_attachment:
            return False
    metadata = getattr(message, "metadata", {}) or {}
    if metadata.get("interactive_reply_id") or metadata.get("postback_payload"):
        return False
    message_type = str(metadata.get("message_type") or "").strip().lower()
    if message_type and message_type not in {"text", "image", "document", "file"}:
        return False
    return True


def _serialize_inbound_message(message: InboundMessage) -> dict:
    """Devuelve el mensaje serialize inbound."""
    return message.model_dump(mode="json")


def _deserialize_inbound_message(payload: dict) -> InboundMessage:
    """Devuelve el mensaje deserialize inbound."""
    return InboundMessage(**payload)


def _merge_messages(messages: list[InboundMessage]) -> InboundMessage:
    """Fusiona messages."""
    if not messages:
        raise ValueError("messages_required")
    base = messages[-1]
    text_parts = [str(item.mensaje or "").strip() for item in messages if str(item.mensaje or "").strip()]
    attachments: list[Attachment] = []
    location: Location | None = None
    cedula = None
    all_ids: list[str] = []
    for item in messages:
        attachments.extend(item.attachments or [])
        if item.location is not None:
            location = item.location
        if item.cedula:
            cedula = item.cedula
        for message_id in _message_ids(item):
            if message_id not in all_ids:
                all_ids.append(message_id)
    merged_text = "\n".join(text_parts).strip() or str(base.mensaje or "").strip()
    metadata = dict(base.metadata or {})
    metadata["coalesced"] = len(messages) > 1
    metadata["coalesced_count"] = len(messages)
    metadata["coalesced_message_ids"] = all_ids
    if all_ids:
        metadata["message_id"] = all_ids[-1]
    return base.model_copy(
        update={
            "mensaje": merged_text,
            "attachments": attachments,
            "location": location,
            "cedula": cedula or base.cedula,
            "metadata": metadata,
        }
    )


async def _flush_coalesced_messages(
    *,
    state,
    sessions,
    orchestrator,
    delivery,
    odoo_chat,
    logger_,
    preview_fn,
):
    """Devuelve los messages flush coalesced."""
    buffer_meta = state.metadata.get("inbound_coalescing") or {}
    payloads = buffer_meta.get("messages") or []
    if not payloads:
        return
    merged = _merge_messages([_deserialize_inbound_message(item) for item in payloads])
    state.metadata.pop("inbound_coalescing", None)
    await sessions.save(state)
    logger_.info(
        "gateway_coalescing_flush session_id=%s channel=%s recipient=%s count=%s preview=%r",
        merged.session_id,
        merged.channel,
        merged.recipient,
        len(payloads),
        preview_fn(merged.mensaje),
    )
    await process_and_deliver(
        merged,
        orchestrator,
        delivery=delivery,
        odoo_chat=odoo_chat,
        logger_=logger_,
        preview_fn=preview_fn,
    )


async def coalesce_and_process(
    message,
    orchestrator,
    *,
    sessions,
    delivery,
    odoo_chat,
    logger_,
    preview_fn,
) -> None:
    """Agrupa mensajes cortos consecutivos antes de entregarlos al orquestador."""
    lock = _coalescing_lock(message)
    if not _should_coalesce_message(message):
        async with lock:
            state = await sessions.load(message)
            if state.metadata.get("inbound_coalescing"):
                buffer_meta = state.metadata.setdefault("inbound_coalescing", {})
                payloads = list(buffer_meta.get("messages") or [])
                payloads.append(_serialize_inbound_message(message))
                buffer_meta["messages"] = payloads
                logger_.info(
                    "gateway_coalescing_flush_early session_id=%s channel=%s recipient=%s reason=special_message count=%s",
                    message.session_id,
                    message.channel,
                    message.recipient,
                    len(payloads),
                )
                await _flush_coalesced_messages(
                    state=state,
                    sessions=sessions,
                    orchestrator=orchestrator,
                    delivery=delivery,
                    odoo_chat=odoo_chat,
                    logger_=logger_,
                    preview_fn=preview_fn,
                )
                return
            await process_and_deliver(
                message,
                orchestrator,
                delivery=delivery,
                odoo_chat=odoo_chat,
                logger_=logger_,
                preview_fn=preview_fn,
            )
            return

    settings = get_settings()
    async with lock:
        state = await sessions.load(message)
        recent_ids = {str(item).strip() for item in (state.metadata.get("recent_inbound_message_ids") or []) if str(item).strip()}
        buffer_meta = state.metadata.setdefault("inbound_coalescing", {})
        payloads = list(buffer_meta.get("messages") or [])
        buffered_ids: set[str] = set()
        for payload in payloads:
            buffered_ids.update(_message_ids(_deserialize_inbound_message(payload)))
        incoming_ids = _message_ids(message)
        if incoming_ids and any(message_id in recent_ids or message_id in buffered_ids for message_id in incoming_ids):
            logger_.info(
                "gateway_coalescing_duplicate session_id=%s channel=%s recipient=%s message_ids=%s",
                message.session_id,
                message.channel,
                message.recipient,
                incoming_ids,
            )
            return

        payloads.append(_serialize_inbound_message(message))
        max_messages = max(1, int(settings.inbound_coalescing_max_messages))
        payloads = payloads[-max_messages:]
        token = uuid.uuid4().hex
        buffer_meta["token"] = token
        buffer_meta["messages"] = payloads
        await sessions.save(state)
        logger_.info(
            "gateway_coalescing_buffered session_id=%s channel=%s recipient=%s count=%s window_seconds=%.2f preview=%r",
            message.session_id,
            message.channel,
            message.recipient,
            len(payloads),
            settings.inbound_coalescing_window_seconds,
            preview_fn(message.mensaje),
        )

    await asyncio.sleep(max(0.0, float(settings.inbound_coalescing_window_seconds)))

    async with lock:
        refreshed = await sessions.load(message)
        refreshed_buffer = refreshed.metadata.get("inbound_coalescing") or {}
        if refreshed_buffer.get("token") != token:
            logger_.info(
                "gateway_coalescing_superseded session_id=%s channel=%s recipient=%s",
                message.session_id,
                message.channel,
                message.recipient,
            )
            return
        await _flush_coalesced_messages(
            state=refreshed,
            sessions=sessions,
            orchestrator=orchestrator,
            delivery=delivery,
            odoo_chat=odoo_chat,
            logger_=logger_,
            preview_fn=preview_fn,
        )


def _should_deliver_audio_reply(*, channel: str, response) -> bool:
    """Indica si reply deliver audio se cumple."""
    settings = get_settings()
    if not settings.audio_enabled or channel != "whatsapp":
        return False
    mode = str(settings.audio_reply_mode or "").strip().lower()
    if mode == "always":
        return True
    if mode != "same_as_input":
        return False
    audio_meta = (getattr(response, "metadata", {}) or {}).get("audio") or {}
    return bool(audio_meta.get("transcribed"))


async def _maybe_build_audio_reply(response, *, channel: str, tts, logger_, preview_fn):
    """Devuelve el reply maybe build audio."""
    if not _should_deliver_audio_reply(channel=channel, response=response):
        return None
    result = await tts.synthesize(response.message, metadata=getattr(response, "metadata", {}) or {})
    if result.get("status") != "ok" or not result.get("media_bytes"):
        logger_.info(
            "gateway_audio_reply_skipped channel=%s status=%s engine=%s voice=%s preview=%r",
            channel,
            result.get("status"),
            result.get("engine"),
            result.get("voice"),
            preview_fn(response.message),
        )
        return None
    token = store_temp_media(
        result["media_bytes"],
        result.get("mime_type"),
        filename=result.get("filename"),
    )
    if not token:
        logger_.warning("gateway_audio_reply_store_failed channel=%s engine=%s", channel, result.get("engine"))
        return None
    return {
        "media_ref": build_public_media_url(token),
        "engine": result.get("engine"),
        "mime_type": result.get("mime_type"),
        "voice": result.get("voice"),
    }


async def process_and_deliver(
    message,
    orchestrator,
    *,
    delivery,
    odoo_chat,
    logger_,
    preview_fn,
) -> None:
    """Procesa deliver and y avanza el flujo."""
    logger_.info(
        "gateway_process_start session_id=%s channel=%s recipient=%s attachments=%s preview=%r",
        message.session_id,
        message.channel,
        message.recipient,
        len(message.attachments or []),
        preview_fn(message.mensaje),
    )
    try:
        response = await orchestrator.handle_message(message)
        if bool((getattr(response, "metadata", {}) or {}).get("skip_delivery")):
            logger_.info(
                "gateway_process_suppressed session_id=%s channel=%s recipient=%s agent=%s intent=%s reason=skip_delivery",
                message.session_id,
                message.channel,
                message.recipient,
                response.agent,
                response.intent,
            )
            return
        tts = _build_text_to_speech_service()
        audio_reply = await _maybe_build_audio_reply(
            response,
            channel=message.channel,
            tts=tts,
            logger_=logger_,
            preview_fn=preview_fn,
        )
        delivery_result = await delivery.deliver(
            channel=message.channel,
            recipient=message.recipient,
            message=audio_reply["media_ref"] if audio_reply else response.message,
            actions=None if audio_reply else response.actions,
            media_type="audio" if audio_reply else None,
        )
        logger_.info(
            "gateway_process_done session_id=%s channel=%s recipient=%s agent=%s intent=%s delivery_status=%s audio_reply=%s response_preview=%r",
            message.session_id,
            message.channel,
            message.recipient,
            response.agent,
            response.intent,
            delivery_result.get("status"),
            bool(audio_reply),
            preview_fn(response.message),
        )
    except Exception as exc:
        logger_.exception(
            "gateway_process_failed session_id=%s channel=%s recipient=%s",
            message.session_id,
            message.channel,
            message.recipient,
        )
        try:
            await odoo_chat.escalate_new_client(
                channel=message.channel,
                recipient=message.recipient,
                summary=(
                    "Fallo del sistema durante el procesamiento o envío del mensaje. "
                    f"session_id={message.session_id}. "
                    f"error={exc.__class__.__name__}: {str(exc)[:240]}"
                ),
                cedula=message.cedula,
                origen="ia",
            )
        except (httpx.HTTPError, RuntimeError, ValueError):
            logger_.exception(
                "gateway_process_handoff_failed session_id=%s channel=%s recipient=%s",
                message.session_id,
                message.channel,
                message.recipient,
            )


async def resolve_runtime_session(message, *, registry, sessions, logger_):
    """Alinea el session_id del mensaje con la sesion persistida real."""
    current_state = await sessions.load(message)
    if _looks_like_human_handoff_state(current_state):
        return message
    resolved = await registry.resolve_session(recipient=message.recipient, red=message.channel)
    existing_session_id = str((resolved or {}).get("session_id") or "").strip()
    if existing_session_id:
        if existing_session_id == message.session_id:
            return message
        logger_.info(
            "gateway_session_resolved recipient=%s channel=%s session_id=%s source=contact_registry",
            message.recipient,
            message.channel,
            existing_session_id,
        )
        return message.model_copy(update={"session_id": existing_session_id})
    cleared = await sessions.clear(recipient=message.recipient)
    new_session_id = uuid.uuid4().hex[:12]
    logger_.info(
        "gateway_session_resolved recipient=%s channel=%s session_id=%s source=new_session stale_cleared=%s",
        message.recipient,
        message.channel,
        new_session_id,
        bool(cleared),
    )
    return message.model_copy(update={"session_id": new_session_id})


async def should_relay_human_message(message, *, registry, sessions) -> bool:
    """Indica si mensaje relay human se cumple."""
    resolved = await registry.resolve_session(recipient=message.recipient, red=message.channel)
    if bool((resolved or {}).get("human_active")):
        return True
    state = await sessions.load(message)
    return _looks_like_human_handoff_state(state)


async def relay_human_message(
    message,
    *,
    contact_registry,
    sessions,
    odoo_chat,
    logger_,
    map_inbound_attachment_tipo,
    resolve_inbound_attachment_reference,
) -> None:
    """Reenvia un mensaje ya normalizado al canal humano de Odoo Chat."""
    resolved = await contact_registry.resolve_session(recipient=message.recipient, red=message.channel)
    state = await sessions.load(message)

    relay_text = str(message.mensaje or "").strip()
    message_type = str(((message.metadata or {}).get("message_type")) or "").strip().lower()
    attachment_placeholders = {
        "image": {"imagen enviada", "image sent"},
        "audio": {"audio enviado", "audio sent"},
        "document": {"documento enviado", "archivo enviado", "document sent", "file sent"},
    }
    normalized_text = " ".join(relay_text.lower().split())
    should_relay_text = bool(relay_text)
    if message.attachments:
        if normalized_text in attachment_placeholders.get(message_type, set()):
            should_relay_text = False

    relayed_any = False
    attachment_payloads: list[dict] = []
    for attachment in message.attachments or []:
        mapped_tipo = map_inbound_attachment_tipo(getattr(attachment, "type", None))
        if not mapped_tipo:
            continue
        raw = attachment.model_dump(mode="json") if hasattr(attachment, "model_dump") else None
        if not isinstance(raw, dict):
            raw = {}
        raw_type = str(raw.get("type") or getattr(attachment, "type", None) or "").strip().lower()
        if raw_type:
            raw["type"] = raw_type
        mime_type = str(raw.get("mime_type") or getattr(attachment, "mime_type", None) or "").strip()
        if mime_type:
            raw["mime_type"] = mime_type
        filename = str(raw.get("filename") or getattr(attachment, "filename", None) or "").strip()
        if filename:
            raw["filename"] = filename
        if not str(raw.get("base64_data") or "").strip():
            reference = resolve_inbound_attachment_reference(attachment)
            if reference:
                raw["url"] = reference
        if str(raw.get("base64_data") or "").strip() or str(raw.get("url") or "").strip():
            attachment_payloads.append(raw)

    await contact_registry.touch_contact(
        recipient=message.recipient,
        red=message.channel,
        identificacion=state.cedula or message.cedula or (resolved or {}).get("cedula"),
        session_id=message.session_id,
        chat_preview=(str(message.mensaje or "").strip() or None),
    )

    common_kwargs = {
        "channel": message.channel,
        "recipient": message.recipient,
        "cedula": state.cedula or message.cedula or (resolved or {}).get("cedula"),
        "origen": "gonet",
        "group": (resolved or {}).get("group"),
        "internal_user": (resolved or {}).get("internal_user"),
        "channel_id": (resolved or {}).get("channel_id"),
    }

    if should_relay_text:
        await odoo_chat.relay_customer_message(
            message=relay_text,
            tipo="texto",
            **common_kwargs,
        )
        relayed_any = True

    if attachment_payloads:
        try:
            results = await odoo_chat.relay_attachments(
                attachments=attachment_payloads,
                **common_kwargs,
            )
            relayed_any = relayed_any or bool(results)
        except Exception:
            logger_.exception(
                "gateway_human_attachment_relay_failed channel=%s recipient=%s attachments=%s",
                message.channel,
                message.recipient,
                len(attachment_payloads),
            )

    if message.location:
        await odoo_chat.relay_customer_message(
            message=message.location.model_dump(exclude_none=True),
            tipo="ubicacion",
            **common_kwargs,
        )
        relayed_any = True

    if not relayed_any:
        state.human_handoff = True
        state.current_intent = "human_handoff"
        state.last_agent = "handoff"
        await sessions.save(state)
        await sessions.touch(
            session_id=message.session_id,
            recipient=message.recipient,
            channel=message.channel,
            actor="user",
            human_handoff=True,
        )
        logger_.info(
            "gateway_human_relay_skipped channel=%s recipient=%s reason=no_relayable_payload",
            message.channel,
            message.recipient,
        )
        return

    state.human_handoff = True
    state.current_intent = "human_handoff"
    state.last_agent = "handoff"
    await sessions.save(state)
    await sessions.touch(
        session_id=message.session_id,
        recipient=message.recipient,
        channel=message.channel,
        actor="user",
        human_handoff=True,
    )
    try:
        await contact_registry.mark_human_active(
            recipient=message.recipient,
            red=message.channel,
            identificacion=state.cedula or message.cedula or (resolved or {}).get("cedula"),
            session_id=message.session_id,
            group=(resolved or {}).get("group"),
            chat_preview=(str(message.mensaje or "").strip() or None),
        )
    except Exception:
        logger_.exception(
            "gateway_human_registry_sync_failed channel=%s recipient=%s",
            message.channel,
            message.recipient,
        )
    logger_.info(
        "gateway_human_relay_done session_id=%s channel=%s recipient=%s text=%s attachments=%s location=%s",
        message.session_id,
        message.channel,
        message.recipient,
        should_relay_text,
        len(message.attachments or []),
        bool(message.location),
    )


async def prime_sales_capture_session(payload: dict, *, store) -> None:
    """Prepara la sesion comercial para capturas CRM antes de enviar."""
    recipient = str(payload.get("recipient") or "").strip()
    if not recipient:
        return
    session_id = str(payload.get("session_id") or recipient).strip()
    state = SessionState(
        session_id=session_id,
        channel=str(payload.get("chanel") or payload.get("channel") or "internal").strip().lower() or "internal",
        recipient=recipient,
        cedula=str(payload.get("cedula") or "").strip() or None,
        current_intent="commercial",
        last_agent="sales",
        metadata={
            "sales": {
                "history": [],
                "lead": {},
                "pending_intent": "commercial",
                "awaiting_agency_location": False,
                "awaiting_crm_field": "partner_name",
                "crm_lead_created": False,
                "commercial_catalog_requested": True,
                "commercial_catalog_segment": None,
                "commercial_handoff_requested": False,
                "last_actions": None,
                "greeted": True,
                "fresh_location": False,
            }
        },
    )
    await store.save(state)


async def dispatch_internal_send(
    payload: dict,
    *,
    delivery,
    odoo_chat,
    contact_registry,
    sessions,
    logger_,
    preview_fn,
    prime_sales_capture_session_cb,
    map_internal_media_type,
    resolve_internal_media_reference_cb,
    resolve_internal_attachment_media_cb,
) -> None:
    """Despacha un envio interno directo al canal o a Odoo Chat."""
    channel = str(payload.get("chanel") or payload.get("channel") or "").strip().lower()
    recipient = str(payload.get("recipient") or "").strip()
    message_text = str(payload.get("message") or payload.get("mensaje") or "").strip()
    original_text = message_text
    odoo_action = str(payload.get("odoo") or "").strip().upper()
    logger_.info(
        "gateway_internal_send_received channel=%s recipient=%s odoo_action=%s media_tipo=%s preview=%r",
        channel,
        recipient,
        odoo_action or None,
        str(payload.get("tipo") or "").strip().lower() or None,
        preview_fn(message_text),
    )

    if odoo_action == "ASISTENTE_ODOO":
        await contact_registry.mark_human_active(
            recipient=recipient,
            red=channel,
            identificacion=str(payload.get("cedula") or "").strip() or None,
            session_id=str(payload.get("session_id") or "").strip() or None,
            group=str(payload.get("group") or "").strip() or "support",
            chat_preview=message_text or None,
        )
        await odoo_chat.escalate_new_client(
            channel=channel,
            recipient=recipient,
            summary=message_text or str(payload.get("summary") or payload.get("detalle") or "").strip() or None,
            cedula=str(payload.get("cedula") or "").strip() or None,
            origen=str(payload.get("origen") or "").strip() or None,
        )
        logger_.info("gateway_internal_send_handoff channel=%s recipient=%s mode=assistant_odoo", channel, recipient)
        return

    if odoo_action == "NUEVO_CLIENTE":
        if message_text.lower() == "asesor":
            group = str(payload.get("group") or "").strip() or "iainfo"
            await contact_registry.mark_human_active(
                recipient=recipient,
                red=channel,
                identificacion=str(payload.get("cedula") or "").strip() or None,
                session_id=str(payload.get("session_id") or "").strip() or None,
                group=group,
                chat_preview=message_text or None,
            )
            await odoo_chat.escalate_new_client(
                channel=channel,
                recipient=recipient,
                summary=None,
                cedula=str(payload.get("cedula") or "").strip() or None,
                origen=str(payload.get("origen") or "").strip() or None,
                group=group,
            )
            logger_.info("gateway_internal_send_handoff channel=%s recipient=%s mode=new_client_advisor", channel, recipient)
            return
        await contact_registry.touch_contact(
            recipient=recipient,
            red=channel,
            identificacion=str(payload.get("cedula") or "").strip() or None,
            session_id=str(payload.get("session_id") or recipient).strip(),
            menu_id="22",
            chat_preview=message_text or None,
        )
        await prime_sales_capture_session_cb(payload)
        await delivery.deliver(
            channel=channel,
            recipient=recipient,
            message=(
                "Hola, te saluda el equipo comercial de GoNet. "
                "Para registrar tu solicitud, compárteme tu nombre completo."
            ),
        )
        logger_.info("gateway_internal_send_seed_sales channel=%s recipient=%s", channel, recipient)
        return

    attachments_payload = payload.get("attachments")
    inline_media_present = bool(
        str(payload.get("base64_data") or "").strip()
        or str(payload.get("url") or "").strip()
        or (
            isinstance(attachments_payload, list)
            and any(
                isinstance(item, dict)
                and (
                    str(item.get("base64_data") or "").strip()
                    or str(item.get("url") or item.get("link") or "").strip()
                )
                for item in attachments_payload
            )
        )
    )
    media_type = map_internal_media_type(payload.get("tipo"))
    media_kind = await resolve_internal_media_reference_cb(payload, channel=channel, media_type=media_type)
    outbound_messages: list[dict] = []
    delivered_media_types: list[str] = []
    if media_type in {"image", "audio", "document"}:
        if media_kind:
            outbound_messages.append({"message": media_kind, "media_type": media_type})
            delivered_media_types.append(media_type)
        if (
            inline_media_present
            and original_text
            and original_text != media_kind
            and not original_text.startswith(("http://", "https://"))
        ):
            outbound_messages.append({"message": original_text, "media_type": None})
    else:
        attachment_media_type, attachment_media_ref = await resolve_internal_attachment_media_cb(payload, channel=channel)
        if attachment_media_type in {"image", "audio", "document"} and attachment_media_ref:
            outbound_messages.append({"message": attachment_media_ref, "media_type": attachment_media_type})
            delivered_media_types.append(attachment_media_type)
        if original_text:
            outbound_messages.append({"message": original_text, "media_type": None})

    if not outbound_messages:
        logger_.info(
            "gateway_internal_send_skipped channel=%s recipient=%s reason=%s",
            channel,
            recipient,
            "unresolved_media_reference" if media_type in {"image", "audio", "document"} else "empty_payload",
        )
        return
    chat_preview = original_text or (
        "Imagen enviada"
        if "image" in delivered_media_types
        else "Audio enviado"
        if "audio" in delivered_media_types
        else "Documento enviado"
        if "document" in delivered_media_types
        else None
    )
    origen = str(payload.get("origen") or "").strip().lower()
    internal_user = payload.get("internal_user")
    channel_id = payload.get("channel_id")
    if str(internal_user or "").strip() or str(channel_id or "").strip():
        await contact_registry.assign_odoo_channel(
            recipient=recipient,
            red=channel,
            internal_user=internal_user,
            channel_id=channel_id,
        )
        if origen not in {"ia", "iainfo"}:
            await contact_registry.mark_human_active(
                recipient=recipient,
                red=channel,
                identificacion=str(payload.get("cedula") or "").strip() or None,
                session_id=str(payload.get("session_id") or "").strip() or None,
                group=str(payload.get("group") or "").strip() or "support",
                chat_preview=chat_preview,
            )
    for item in outbound_messages:
        await delivery.deliver(
            channel=channel,
            recipient=recipient,
            message=item["message"],
            actions=payload.get("actions") if item["media_type"] is None else None,
            media_type=item["media_type"],
        )
    if origen in {"ia", "iainfo"}:
        await contact_registry.mark_ai_active(
            recipient=recipient,
            red=channel,
            identificacion=str(payload.get("cedula") or "").strip() or None,
            session_id=str(payload.get("session_id") or "").strip() or None,
            group="iainfo" if origen == "iainfo" else "support",
            chat_preview=chat_preview,
        )
    else:
        await contact_registry.touch_contact(
            recipient=recipient,
            red=channel,
            identificacion=str(payload.get("cedula") or "").strip() or None,
            session_id=str(payload.get("session_id") or "").strip() or None,
            chat_preview=chat_preview,
        )
    await sessions.touch(
        session_id=str(payload.get("session_id") or "").strip() or None,
        recipient=recipient or None,
        channel=channel or None,
        actor="assistant",
        human_handoff=True if str(internal_user or "").strip() or str(channel_id or "").strip() else None,
    )
    logger_.info(
        "gateway_internal_send_delivered channel=%s recipient=%s media_types=%s text=%s",
        channel,
        recipient,
        delivered_media_types or None,
        any(item["media_type"] is None for item in outbound_messages),
    )
