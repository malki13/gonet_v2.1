"""Normalizacion de eventos crudos de Meta a `InboundMessage`."""

import base64
from typing import Any

import httpx

from packages.shared.config import get_settings
from packages.shared.schemas import Attachment, InboundMessage, Location, MetaWebhookPayload


def normalize_meta_payload(payload: MetaWebhookPayload) -> InboundMessage:
    """Normaliza payload meta."""
    return InboundMessage(
        mensaje=payload.mensaje,
        channel=payload.channel,
        recipient=payload.recipient,
        session_id=payload.session_id,
        cedula=payload.cedula,
        attachments=payload.attachments,
        metadata={"source": "meta_webhook"},
    )


def extract_meta_verification(query_params: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Extrae meta verification."""
    return (
        str(query_params.get("hub.mode") or "").strip() or None,
        str(query_params.get("hub.verify_token") or "").strip() or None,
        str(query_params.get("hub.challenge") or "").strip() or None,
    )


async def normalize_meta_event(payload: dict[str, Any]) -> list[InboundMessage]:
    """Normaliza event meta."""
    object_type = str(payload.get("object") or "").strip()
    entries = payload.get("entry") or []
    if object_type == "whatsapp_business_account":
        return await _normalize_whatsapp_entries(entries)
    if object_type == "page":
        return _normalize_messenger_entries(entries)
    return []


async def _normalize_whatsapp_entries(entries: list[dict[str, Any]]) -> list[InboundMessage]:
    """Normaliza entries whatsapp."""
    messages: list[InboundMessage] = []
    for entry in entries or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for item in value.get("messages") or []:
                normalized = await _normalize_whatsapp_message(item)
                if normalized is not None:
                    messages.append(normalized)
    return messages


def _normalize_messenger_entries(entries: list[dict[str, Any]]) -> list[InboundMessage]:
    """Normaliza entries messenger."""
    messages: list[InboundMessage] = []
    for entry in entries or []:
        for item in entry.get("messaging") or []:
            normalized = _normalize_messenger_message(item)
            if normalized is not None:
                messages.append(normalized)
    return messages


def _whatsapp_text(message: dict[str, Any]) -> str:
    """Devuelve el texto whatsapp."""
    message_type = str(message.get("type") or "").strip().lower()
    if message_type == "text":
        return str(((message.get("text") or {}).get("body") or "")).strip()
    if message_type == "interactive":
        interactive = message.get("interactive") or {}
        button_reply = interactive.get("button_reply") or {}
        list_reply = interactive.get("list_reply") or {}
        return str(button_reply.get("title") or list_reply.get("title") or button_reply.get("id") or list_reply.get("id") or "").strip()
    if message_type == "button":
        return str(((message.get("button") or {}).get("text") or "")).strip()
    if message_type == "location":
        return "Ubicación compartida"
    if message_type == "image":
        return str(((message.get("image") or {}).get("caption") or "Imagen enviada")).strip()
    if message_type == "document":
        return str(((message.get("document") or {}).get("caption") or "Documento enviado")).strip()
    if message_type == "audio":
        return "Audio enviado"
    return ""


async def _normalize_whatsapp_message(message: dict[str, Any]) -> InboundMessage | None:
    """Normaliza mensaje whatsapp."""
    sender = str(message.get("from") or "").strip()
    if not sender:
        return None
    message_type = str(message.get("type") or "").strip().lower()
    attachments: list[Attachment] = []
    location: Location | None = None
    if message_type in {"image", "document", "audio"}:
        attachment = await _build_whatsapp_attachment(message_type, message)
        if attachment is not None:
            attachments.append(attachment)
    elif message_type == "location":
        raw_location = message.get("location") or {}
        location = Location(
            latitude=raw_location.get("latitude"),
            longitude=raw_location.get("longitude"),
            address=raw_location.get("address") or raw_location.get("name"),
        )

    return InboundMessage(
        mensaje=_whatsapp_text(message),
        channel="whatsapp",
        recipient=sender,
        session_id=sender,
        attachments=attachments,
        location=location,
        metadata={
            "source": "meta_whatsapp_raw",
            "message_id": message.get("id"),
            "message_type": message_type,
            "interactive_reply_id": str((((message.get("interactive") or {}).get("button_reply") or {}).get("id") or ((message.get("interactive") or {}).get("list_reply") or {}).get("id") or "")).strip() or None,
            "interactive_reply_title": str((((message.get("interactive") or {}).get("button_reply") or {}).get("title") or ((message.get("interactive") or {}).get("list_reply") or {}).get("title") or "")).strip() or None,
        },
    )


def _normalize_messenger_message(item: dict[str, Any]) -> InboundMessage | None:
    """Normaliza mensaje messenger."""
    sender = str(((item.get("sender") or {}).get("id") or "")).strip()
    if not sender:
        return None
    message = item.get("message") or {}
    postback = item.get("postback") or {}
    if message.get("is_echo"):
        return None

    attachments: list[Attachment] = []
    location: Location | None = None
    for attachment in message.get("attachments") or []:
        attachment_type = str(attachment.get("type") or "").strip().lower()
        if attachment_type == "location":
            coords = ((attachment.get("payload") or {}).get("coordinates") or {})
            location = Location(
                latitude=coords.get("lat"),
                longitude=coords.get("long"),
            )
        elif attachment_type in {"image", "audio", "file", "video"}:
            payload = attachment.get("payload") or {}
            attachments.append(
                Attachment(
                    type=attachment_type,
                    url=payload.get("url"),
                    mime_type=payload.get("mime_type"),
                )
            )

    text = str(message.get("text") or postback.get("title") or postback.get("payload") or "").strip()
    return InboundMessage(
        mensaje=text or ("Ubicación compartida" if location else "Adjunto recibido"),
        channel="messenger",
        recipient=sender,
        session_id=sender,
        attachments=attachments,
        location=location,
        metadata={
            "source": "meta_messenger_raw",
            "message_id": message.get("mid"),
            "postback_payload": postback.get("payload"),
        },
    )


async def _build_whatsapp_attachment(kind: str, message: dict[str, Any]) -> Attachment | None:
    """Construye adjunto whatsapp a partir del contexto disponible."""
    settings = get_settings()
    token = settings.token_whatsapp or settings.whatsapp_media_token
    payload = message.get(kind) or {}
    media_id = str(payload.get("id") or "").strip()
    if not media_id or not token:
        return None

    info_url = f"https://graph.facebook.com/{settings.whatsapp_graph_version}/{media_id}"
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        info_response = await client.get(info_url, headers=headers)
        info_response.raise_for_status()
        info_body = info_response.json()
        media_url = str(info_body.get("url") or "").strip()
        if not media_url:
            return None
        file_response = await client.get(media_url, headers=headers)
        file_response.raise_for_status()
        media_bytes = file_response.content
        mime_type = str(payload.get("mime_type") or file_response.headers.get("Content-Type") or "").split(";")[0].strip()
        return Attachment(
            type=kind,
            mime_type=mime_type or None,
            url=media_url,
            base64_data=base64.b64encode(media_bytes).decode("ascii"),
            filename=payload.get("filename") or f"{kind}_{media_id}",
        )
