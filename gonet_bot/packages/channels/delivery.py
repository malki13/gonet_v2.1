"""Entrega final de mensajes por canal externo."""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from packages.channels.media_proxy import resolve_temp_media
from packages.shared.config import get_settings

logger = logging.getLogger("channels.delivery")


def _message_body(value: Any) -> str:
    """Devuelve el body mensaje."""
    return str(value or "").strip()


def _whatsapp_media_ref_payload(kind: str, media_ref: str) -> dict[str, str]:
    """Devuelve el payload whatsapp media ref."""
    clean_ref = _message_body(media_ref)
    if clean_ref.startswith(("http://", "https://")):
        return {kind: {"link": clean_ref}}
    return {kind: {"id": clean_ref}}


def _normalize_whatsapp_media_mime(*, kind: str, mime_type: str | None, media_ref: str) -> str:
    """Normaliza el MIME antes de subir media a WhatsApp."""
    normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
    if kind != "audio":
        return normalized or "application/octet-stream"
    if normalized in {"audio/ogg", "audio/opus", "audio/oga"}:
        return "audio/ogg"
    if normalized in {"audio/mpeg", "audio/mp3", "audio/x-mpeg", "audio/x-mp3"}:
        return "audio/mpeg"
    if normalized in {"audio/mp4", "audio/m4a", "audio/aac", "audio/x-m4a"}:
        return "audio/mp4"
    suffix = Path(urlparse(media_ref).path).suffix.lower()
    if suffix in {".ogg", ".opus", ".oga"}:
        return "audio/ogg"
    if suffix in {".mp3", ".mpeg"}:
        return "audio/mpeg"
    if suffix in {".m4a", ".aac"}:
        return "audio/mp4"
    return normalized or "audio/mpeg"


def _extract_buttons(actions: Any) -> list[dict[str, str]]:
    """Extrae buttons."""
    if not isinstance(actions, dict):
        return []
    if str(actions.get("type") or "").strip().lower() != "buttons":
        return []
    out: list[dict[str, str]] = []
    for idx, item in enumerate(actions.get("buttons") or [], start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        payload = str(item.get("id") or item.get("value") or idx).strip()
        out.append({"id": payload, "title": title})
    return out


class ChannelDeliveryService:
    """Traduce una respuesta interna al formato esperado por WhatsApp o Messenger."""

    def __init__(self) -> None:
        """Inicializa el channeldeliveryservice con la configuracion necesaria."""
        self.settings = get_settings()

    def _is_dry_run(self) -> bool:
        """Indica si run dry se cumple."""
        return bool(self.settings.dry_run_externals)

    def _ffmpeg_bin(self) -> str | None:
        """Devuelve el binario ffmpeg disponible para conversiones salientes."""
        configured = str(self.settings.audio_ffmpeg_bin or "").strip()
        if configured:
            return configured
        return shutil.which("ffmpeg")

    @staticmethod
    def _extract_media_proxy_token(media_ref: str) -> str | None:
        """Extrae el token si la referencia apunta al proxy `/media/{token}`."""
        clean_ref = _message_body(media_ref)
        if not clean_ref.startswith(("http://", "https://")):
            return None
        try:
            parsed = urlparse(clean_ref)
        except Exception:
            return None
        path = str(parsed.path or "").strip()
        if not path.startswith("/media/"):
            return None
        token = path.rsplit("/", 1)[-1].strip()
        return token or None

    @staticmethod
    def _guess_filename(kind: str, media_ref: str, mime_type: str | None) -> str:
        """Construye un nombre de archivo estable para subir media a WhatsApp."""
        token = ChannelDeliveryService._extract_media_proxy_token(media_ref)
        if token:
            return token
        suffix = Path(urlparse(media_ref).path).suffix if media_ref.startswith(("http://", "https://")) else ""
        if suffix:
            return f"{kind}{suffix}"
        if str(mime_type or "").startswith("image/"):
            return f"{kind}.jpg"
        if str(mime_type or "").startswith("audio/"):
            return f"{kind}.ogg"
        if str(mime_type or "").startswith("application/pdf"):
            return f"{kind}.pdf"
        return f"{kind}.bin"

    async def _media_bytes_from_ref(self, media_ref: str, kind: str) -> tuple[bytes, str | None, str] | None:
        """Carga bytes desde el proxy local o desde una URL remota."""
        token = self._extract_media_proxy_token(media_ref)
        if token:
            file_path, meta = resolve_temp_media(token)
            if file_path and meta:
                return (
                    file_path.read_bytes(),
                    str(meta.get("mime_type") or "").strip() or None,
                    str(meta.get("filename") or token).strip() or token,
                )
        clean_ref = _message_body(media_ref)
        if not clean_ref.startswith(("http://", "https://")):
            return None
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(clean_ref)
            response.raise_for_status()
            mime_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip() or None
            filename = self._guess_filename(kind, clean_ref, mime_type)
            return response.content, mime_type, filename

    def _prepare_whatsapp_audio_upload(
        self,
        *,
        media_bytes: bytes,
        mime_type: str | None,
        filename: str,
    ) -> tuple[bytes, str, str]:
        """Convierte el audio a OGG/Opus cuando hay ffmpeg disponible."""
        ffmpeg_bin = self._ffmpeg_bin()
        if not ffmpeg_bin or not media_bytes:
            normalized_mime = _normalize_whatsapp_media_mime(kind="audio", mime_type=mime_type, media_ref=filename)
            return media_bytes, normalized_mime, filename

        input_suffix = Path(filename).suffix or ".bin"
        output_name = f"{Path(filename).stem or 'audio'}.ogg"
        input_file = tempfile.NamedTemporaryFile(suffix=input_suffix, delete=False)
        output_file = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
        input_path = Path(input_file.name)
        output_path = Path(output_file.name)
        input_file.close()
        output_file.close()
        try:
            input_path.write_bytes(media_bytes)
            command = [
                ffmpeg_bin,
                "-y",
                "-i",
                str(input_path),
                "-vn",
                "-c:a",
                "libopus",
                "-b:a",
                "48k",
                "-ac",
                "1",
                str(output_path),
            ]
            completed = subprocess.run(command, capture_output=True, check=False)
            if completed.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                return output_path.read_bytes(), "audio/ogg", output_name
            logger.warning(
                "delivery_whatsapp_audio_transcode_failed filename=%s returncode=%s",
                filename,
                completed.returncode,
            )
        except OSError:
            logger.exception("delivery_whatsapp_audio_transcode_spawn_failed filename=%s", filename)
        finally:
            input_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)
        normalized_mime = _normalize_whatsapp_media_mime(kind="audio", mime_type=mime_type, media_ref=filename)
        return media_bytes, normalized_mime, filename

    def _whatsapp_media_upload_url(self) -> str | None:
        """Deriva el endpoint `/media` a partir de `URL_WPP`."""
        base = str(self.settings.url_wpp or "").strip()
        if not base:
            return None
        if base.endswith("/messages"):
            return f"{base[:-len('/messages')]}/media"
        return None

    async def _upload_whatsapp_media(self, *, kind: str, media_ref: str) -> str | None:
        """Sube el binario a Meta para enviar el mensaje por `id` en vez de `link`."""
        upload_url = self._whatsapp_media_upload_url()
        token = self.settings.token_whatsapp or self.settings.whatsapp_media_token
        if not upload_url or not token:
            return None
        media_payload = await self._media_bytes_from_ref(media_ref, kind)
        if not media_payload:
            return None
        media_bytes, mime_type, filename = media_payload
        if kind == "audio":
            media_bytes, mime_type, filename = self._prepare_whatsapp_audio_upload(
                media_bytes=media_bytes,
                mime_type=mime_type,
                filename=filename,
            )
        headers = {"Authorization": f"Bearer {token}"}
        upload_mime = _normalize_whatsapp_media_mime(kind=kind, mime_type=mime_type, media_ref=media_ref)
        files = {
            "messaging_product": (None, "whatsapp"),
            "file": (filename, media_bytes, upload_mime),
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(upload_url, headers=headers, files=files)
            response.raise_for_status()
            body = response.json()
        media_id = str(body.get("id") or "").strip()
        return media_id or None

    async def deliver(
        self,
        *,
        channel: str,
        recipient: str,
        message: str,
        actions: Any = None,
        media_type: str | None = None,
    ) -> dict:
        """Despacha el mensaje al canal indicado y devuelve el resultado."""
        channel = str(channel or "").strip().lower()
        logger.info(
            "delivery_start channel=%s recipient=%s media_type=%s actions=%s preview=%r",
            channel,
            recipient,
            media_type,
            str((actions or {}).get("type")) if isinstance(actions, dict) else None,
            _message_body(message)[:160],
        )
        if channel == "whatsapp":
            if media_type == "image":
                result = await self._send_whatsapp_media(recipient, "image", message)
                logger.info("delivery_done channel=%s recipient=%s status=%s", channel, recipient, result.get("status"))
                return result
            if media_type == "audio":
                result = await self._send_whatsapp_media(recipient, "audio", message)
                logger.info("delivery_done channel=%s recipient=%s status=%s", channel, recipient, result.get("status"))
                return result
            if media_type == "document":
                result = await self._send_whatsapp_media(recipient, "document", message)
                logger.info("delivery_done channel=%s recipient=%s status=%s", channel, recipient, result.get("status"))
                return result
            result = await self._send_whatsapp(recipient, message, actions)
            logger.info("delivery_done channel=%s recipient=%s status=%s", channel, recipient, result.get("status"))
            return result
        if channel == "messenger":
            if media_type == "image":
                result = await self._send_messenger_image(recipient, message)
                logger.info("delivery_done channel=%s recipient=%s status=%s", channel, recipient, result.get("status"))
                return result
            result = await self._send_messenger(recipient, message, actions)
            logger.info("delivery_done channel=%s recipient=%s status=%s", channel, recipient, result.get("status"))
            return result
        raise ValueError(f"unsupported_channel:{channel}")

    async def _post(self, url: str, *, headers: dict[str, str], payload: dict) -> dict:
        """Envía una peticion `POST` y devuelve la respuesta."""
        if self._is_dry_run():
            return {"status": "dry_run", "url": url, "payload": payload}
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            try:
                return {"status": "sent", "payload": payload, "response": response.json()}
            except Exception:
                return {"status": "sent", "payload": payload, "response_text": response.text}

    async def _send_whatsapp(self, recipient: str, message: str, actions: Any) -> dict:
        """Envía whatsapp."""
        if not self.settings.url_wpp or not self.settings.token_whatsapp:
            return {"status": "skipped", "reason": "missing_whatsapp_config"}
        buttons = _extract_buttons(actions)
        headers = {
            "Authorization": f"Bearer {self.settings.token_whatsapp}",
            "Content-Type": "application/json",
        }
        clean_message = _message_body(message)
        if buttons:
            if len(buttons) <= 3:
                payload = {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": recipient,
                    "type": "interactive",
                    "interactive": {
                        "type": "button",
                        "body": {"text": clean_message or "Selecciona una opción"},
                        "action": {
                            "buttons": [
                                {
                                    "type": "reply",
                                    "reply": {
                                        "id": item["id"],
                                        "title": item["title"][:20],
                                    },
                                }
                                for item in buttons
                            ]
                        },
                    },
                }
            else:
                payload = {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": recipient,
                    "type": "interactive",
                    "interactive": {
                        "type": "list",
                        "body": {"text": clean_message or "Selecciona una opción"},
                        "action": {
                            "button": "Ver opciones",
                            "sections": [
                                {
                                    "title": "Opciones",
                                    "rows": [
                                        {
                                            "id": item["id"],
                                            "title": item["title"][:24],
                                        }
                                        for item in buttons[:10]
                                    ],
                                }
                            ],
                        },
                    },
                }
        else:
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "text",
                "text": {"preview_url": False, "body": clean_message},
            }
        return await self._post(self.settings.url_wpp, headers=headers, payload=payload)

    async def _send_whatsapp_media(self, recipient: str, kind: str, media_ref: str) -> dict:
        """Envía media de whatsapp."""
        if not self.settings.url_wpp or not self.settings.token_whatsapp:
            return {"status": "skipped", "reason": "missing_whatsapp_config"}
        headers = {
            "Authorization": f"Bearer {self.settings.token_whatsapp}",
            "Content-Type": "application/json",
        }
        media_id = None
        clean_ref = _message_body(media_ref)
        if clean_ref.startswith(("http://", "https://")):
            try:
                media_id = await self._upload_whatsapp_media(kind=kind, media_ref=clean_ref)
            except Exception:
                logger.exception("delivery_whatsapp_media_upload_failed recipient=%s kind=%s ref=%s", recipient, kind, clean_ref)
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": kind,
            **_whatsapp_media_ref_payload(kind, media_id or media_ref),
        }
        return await self._post(self.settings.url_wpp, headers=headers, payload=payload)

    async def _send_messenger(self, recipient: str, message: str, actions: Any) -> dict:
        """Envía messenger."""
        if not self.settings.url_msg or not self.settings.page_access_token:
            return {"status": "skipped", "reason": "missing_messenger_config"}
        headers = {
            "Authorization": f"Bearer {self.settings.page_access_token}",
            "Content-Type": "application/json",
        }
        buttons = _extract_buttons(actions)
        clean_message = _message_body(message)
        if buttons:
            payload = {
                "recipient": {"id": recipient},
                "message": {
                    "attachment": {
                        "type": "template",
                        "payload": {
                            "template_type": "button",
                            "text": clean_message or "Selecciona una opción",
                            "buttons": [
                                {
                                    "type": "postback",
                                    "title": item["title"][:20],
                                    "payload": item["id"],
                                }
                                for item in buttons[:3]
                            ],
                        },
                    }
                },
            }
        else:
            payload = {
                "recipient": {"id": recipient},
                "message": {"text": clean_message},
            }
        return await self._post(self.settings.url_msg, headers=headers, payload=payload)

    async def _send_messenger_image(self, recipient: str, media_ref: str) -> dict:
        """Envía messenger image."""
        if not self.settings.url_msg or not self.settings.page_access_token:
            return {"status": "skipped", "reason": "missing_messenger_config"}
        headers = {
            "Authorization": f"Bearer {self.settings.page_access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "recipient": {"id": recipient},
            "message": {
                "attachment": {
                    "type": "image",
                    "payload": {"url": media_ref, "is_reusable": False},
                }
            },
        }
        return await self._post(self.settings.url_msg, headers=headers, payload=payload)
