"""Cliente para derivaciones y reenvio de mensajes hacia Odoo Chat."""

import logging

import httpx

from packages.channels.media_proxy import build_public_media_url, store_temp_base64_media
from packages.integrations.contact_registry import build_contact_registry
from packages.shared.config import get_settings

logger = logging.getLogger("integrations.odoo_chat")


class OdooChatClient:
    """Cliente para derivaciones y reenvio de mensajes hacia Odoo Chat."""

    def __init__(self) -> None:
        """Inicializa el odoochatclient con la configuracion necesaria."""
        self.settings = get_settings()

    def _direct_target_url(self) -> str | None:
        """Devuelve el URL direct target."""
        return self.settings.url_odoo_chat

    @staticmethod
    def _normalize_group(group: str | None, origen: str | None) -> str:
        """Normaliza grupo."""
        if group:
            return group
        return "support"

    @staticmethod
    def _normalize_origen(origen: str | None) -> str:
        """Normaliza origen."""
        raw = (origen or "").strip().lower()
        if raw in {"", "gonet", "ia", "iainfo"}:
            return "gonet"
        if raw == "gomax":
            return "gomax"
        return raw

    @staticmethod
    def _attachment_tipo(attachment: dict) -> str | None:
        """Devuelve el tipo adjunto."""
        raw_type = str(attachment.get("type") or "").strip().lower()
        mime_type = str(attachment.get("mime_type") or "").strip().lower()
        if raw_type == "image" or mime_type.startswith("image/"):
            return "imagen"
        if raw_type == "audio" or mime_type.startswith("audio/"):
            return "audio"
        if raw_type in {"document", "file"} or mime_type in {"application/pdf"} or mime_type.startswith("application/"):
            return "documento"
        return None

    def _attachment_reference(self, attachment: dict) -> str | None:
        """Devuelve el reference adjunto."""
        url = str(attachment.get("url") or "").strip()
        base64_data = str(attachment.get("base64_data") or "").strip()
        mime_type = str(attachment.get("mime_type") or "").strip() or None
        filename = str(attachment.get("filename") or "").strip() or None
        if base64_data:
            token = store_temp_base64_media(base64_data, mime_type, filename=filename)
            if token:
                return build_public_media_url(token)
        if url.startswith(("http://", "https://")):
            return url
        return None

    @staticmethod
    def _normalize_assignment(
        *,
        internal_user: str | int | None,
        channel_id: str | int | None,
    ) -> tuple[int | None, int | None]:
        """Normaliza assignment."""
        parsed_internal = int(str(internal_user).strip()) if str(internal_user or "").strip() else None
        parsed_channel = int(str(channel_id).strip()) if str(channel_id or "").strip() else None
        return parsed_internal, parsed_channel

    @staticmethod
    def _json_error_message(body) -> str | None:
        """Devuelve el mensaje json error."""
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "").strip()
                detail = str((error.get("data") or {}).get("message") or "").strip()
                combined = detail or message
                return combined or "odoo_chat_remote_error"
            result = body.get("result")
            if isinstance(result, dict) and str(result.get("status") or "").strip().lower() == "error":
                message = str(result.get("message") or "").strip()
                return message or "odoo_chat_remote_error"
        return None

    async def _post_json(self, target_url: str, payload: dict) -> dict:
        """Devuelve el json post."""
        logger.info(
            "odoo_chat_post target=%s recipient=%s channel=%s group=%s tipo=%s summary=%r",
            target_url,
            payload.get("recipient"),
            payload.get("chanel"),
            payload.get("group"),
            payload.get("tipo"),
            str(payload.get("message") or payload.get("summary") or "")[:160],
        )
        if self.settings.dry_run_externals:
            return {"status": "dry_run", "payload": payload, "target_url": target_url}
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(target_url, json=payload)
            response.raise_for_status()
            try:
                body = response.json()
            except Exception:
                body = response.text
        remote_error = self._json_error_message(body)
        if remote_error:
            logger.warning(
                "odoo_chat_post_error target=%s recipient=%s tipo=%s error=%s",
                target_url,
                payload.get("recipient"),
                payload.get("tipo"),
                remote_error,
            )
            raise RuntimeError(remote_error)
        internal_user, channel_id = self._extract_assignment(body)
        await self._sync_assignment(payload=payload, body=body)
        logger.info("odoo_chat_post_done target=%s recipient=%s status=%s", target_url, payload.get("recipient"), response.status_code)
        return {
            "status": "sent",
            "payload": payload,
            "target_url": target_url,
            "response": body,
            "internal_user": internal_user,
            "channel_id": channel_id,
        }

    @staticmethod
    def _extract_assignment(body) -> tuple[int | None, int | None]:
        """Extrae assignment."""
        candidates: list[dict] = []
        if isinstance(body, dict):
            candidates.append(body)
            for key in ("result", "data", "payload"):
                nested = body.get(key)
                if isinstance(nested, dict):
                    candidates.append(nested)
        for candidate in candidates:
            internal_user = candidate.get("internal_user") or candidate.get("user_id")
            channel_id = candidate.get("channel_id") or candidate.get("id_channel")
            try:
                parsed_internal = int(str(internal_user).strip()) if str(internal_user or "").strip() else None
            except Exception:
                parsed_internal = None
            try:
                parsed_channel = int(str(channel_id).strip()) if str(channel_id or "").strip() else None
            except Exception:
                parsed_channel = None
            if parsed_internal is not None or parsed_channel is not None:
                return parsed_internal, parsed_channel
        return None, None

    async def _sync_assignment(self, *, payload: dict, body) -> None:
        """Devuelve el assignment sync."""
        recipient = str(payload.get("recipient") or "").strip()
        channel = str(payload.get("chanel") or payload.get("channel") or "").strip().lower()
        if not recipient or channel not in {"whatsapp", "messenger"}:
            return
        internal_user, channel_id = self._extract_assignment(body)
        if internal_user is None and channel_id is None:
            return
        registry = build_contact_registry()
        await registry.assign_odoo_channel(
            recipient=recipient,
            red=channel,
            internal_user=internal_user,
            channel_id=channel_id,
        )

    async def _send_direct_odoo_chat(
        self,
        *,
        channel: str,
        recipient: str,
        message,
        cedula: str | None = None,
        origen: str | None = None,
        group: str | None = None,
        tipo: str = "texto",
        internal_user: str | int | None = None,
        channel_id: str | int | None = None,
        extra_payload: dict | None = None,
    ) -> dict:
        """Envía direct odoo chat."""
        target_url = self._direct_target_url()
        if not target_url:
            return {"status": "skipped", "reason": "missing_url_odoo_chat"}
        normalized_origen = self._normalize_origen(origen)
        parsed_internal_user, parsed_channel_id = self._normalize_assignment(
            internal_user=internal_user,
            channel_id=channel_id,
        )
        if (str(internal_user or "").strip() or str(channel_id or "").strip()) and (
            parsed_internal_user is None or parsed_channel_id is None
        ):
            logger.info(
                "odoo_chat_partial_assignment recipient=%s channel=%s internal_user=%r channel_id=%r",
                recipient,
                channel,
                parsed_internal_user,
                parsed_channel_id,
            )
        payload = {
            "token": self.settings.odoo_public_token,
            "client_email": self.settings.odoo_client_email,
            "vat": cedula or recipient or "",
            "message": message,
            "chanel": channel,
            "recipient": recipient,
            "group": self._normalize_group(group, normalized_origen),
            "origen": normalized_origen,
            "tipo": tipo,
        }
        if parsed_internal_user is not None:
            payload["internal_user"] = parsed_internal_user
        if parsed_channel_id is not None:
            payload["channel_id"] = parsed_channel_id
        if isinstance(extra_payload, dict):
            for key, value in extra_payload.items():
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                payload[key] = value
        return await self._post_json(target_url, payload)

    async def relay_customer_message(
        self,
        *,
        channel: str,
        recipient: str,
        message,
        tipo: str = "texto",
        cedula: str | None = None,
        origen: str | None = None,
        group: str | None = None,
        internal_user: str | int | None = None,
        channel_id: str | int | None = None,
    ) -> dict:
        """Reenvia el mensaje del cliente al chat humano de Odoo."""
        return await self._send_direct_odoo_chat(
            channel=channel,
            recipient=recipient,
            message=message,
            cedula=cedula,
            origen=origen or "gonet",
            group=group,
            tipo=tipo,
            internal_user=internal_user,
            channel_id=channel_id,
        )

    async def _routing_context(
        self,
        *,
        channel: str,
        recipient: str,
        group: str | None = None,
        internal_user: str | int | None = None,
        channel_id: str | int | None = None,
    ) -> dict:
        """Devuelve el contexto routing."""
        parsed_internal_user, parsed_channel_id = self._normalize_assignment(
            internal_user=internal_user,
            channel_id=channel_id,
        )
        resolved_group = str(group or "").strip() or None
        if parsed_internal_user is not None and parsed_channel_id is not None:
            return {
                "internal_user": parsed_internal_user,
                "channel_id": parsed_channel_id,
                "group": resolved_group,
            }
        registry = build_contact_registry()
        resolved = await registry.resolve_session(recipient=recipient, red=channel)
        resolved_internal_user, resolved_channel_id = self._normalize_assignment(
            internal_user=resolved.get("internal_user"),
            channel_id=resolved.get("channel_id"),
        )
        return {
            "internal_user": resolved_internal_user,
            "channel_id": resolved_channel_id,
            "group": resolved_group or resolved.get("group"),
        }

    async def relay_attachments(
        self,
        *,
        channel: str,
        recipient: str,
        attachments: list[dict] | None,
        cedula: str | None = None,
        origen: str | None = None,
        group: str | None = None,
        internal_user: str | int | None = None,
        channel_id: str | int | None = None,
    ) -> list[dict]:
        """Reenvia adjuntos del cliente a Odoo Chat usando el canal actual."""
        routing = await self._routing_context(
            channel=channel,
            recipient=recipient,
            group=group,
            internal_user=internal_user,
            channel_id=channel_id,
        )
        results: list[dict] = []
        for item in attachments or []:
            if not isinstance(item, dict):
                continue
            tipo = self._attachment_tipo(item)
            reference = self._attachment_reference(item)
            if not tipo or not reference:
                continue
            results.append(
                await self._send_direct_odoo_chat(
                    channel=channel,
                    recipient=recipient,
                    message=reference,
                    cedula=cedula,
                    origen=origen,
                    group=routing.get("group"),
                    tipo=tipo,
                    internal_user=routing.get("internal_user"),
                    channel_id=routing.get("channel_id"),
                    extra_payload={
                        "media_ref": reference,
                        "base64_data": str(item.get("base64_data") or "").strip() or None,
                        "mime_type": str(item.get("mime_type") or "").strip() or None,
                        "filename": str(item.get("filename") or "").strip() or None,
                    },
                )
            )
        return results

    async def create_handoff(
        self,
        summary: str,
        *,
        channel: str = "internal",
        recipient: str = "",
        cedula: str | None = None,
        origen: str | None = None,
        group: str | None = None,
    ) -> dict:
        """Devuelve el handoff create."""
        return await self._send_direct_odoo_chat(
            channel=channel,
            recipient=recipient,
            message=summary,
            cedula=cedula,
            origen=origen,
            group=group,
        )

    async def escalate_new_client(
        self,
        *,
        channel: str,
        recipient: str,
        summary: str | None = None,
        cedula: str | None = None,
        origen: str | None = None,
        group: str | None = None,
    ) -> dict:
        """Escala un cliente nuevo o una solicitud comercial hacia Odoo Chat."""
        message = summary
        if not message:
            if (origen or self.settings.info_origen) == self.settings.info_origen:
                message = "Nuevo cliente interesado en informacion comercial."
            else:
                message = "Cliente solicita derivación con asesor especializado."
        return await self._send_direct_odoo_chat(
            channel=channel,
            recipient=recipient,
            message=message,
            cedula=cedula,
            origen=origen or self.settings.info_origen,
            group=group,
        )

    async def notify_channel_closed(
        self,
        *,
        channel: str,
        recipient: str,
        cedula: str | None = None,
        group: str | None = None,
        origen: str | None = None,
    ) -> dict:
        """Notifica a Odoo Chat que el canal del cliente fue cerrado."""
        message = "El canal del usuario se ha cerrado"
        return await self._send_direct_odoo_chat(
            channel=channel,
            recipient=recipient,
            message=message,
            cedula=cedula,
            origen=origen or "gonet",
            group=group,
        )
