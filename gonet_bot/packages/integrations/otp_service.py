"""Servicio de OTP para flujos de verificacion y cambio de redes."""

import asyncio
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone

from packages.integrations.contract_lookup import ContractLookupClient
from packages.integrations.postgres_otp import get_last_otp, insert_otp
from packages.integrations.runtime import get_redis_client
from packages.integrations.smtp import SMTPClient
from packages.shared.config import get_settings

logger = logging.getLogger("otp_service")


def _key(prefix: str, recipient: str, session_id: str) -> str:
    """Devuelve la clave usada para agrupar o persistir datos."""
    return f"otp:{recipient}:{session_id}:{prefix}"


def _gen_otp_hex(n: int) -> str:
    """Devuelve el hex gen otp."""
    alphabet = "0123456789ABCDEF"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _otp_hmac(otp: str, recipient: str, session_id: str, secret: str) -> str:
    """Devuelve el hmac otp."""
    key = f"{secret}:{recipient}:{session_id}".encode("utf-8")
    return hmac.new(key, otp.encode("utf-8"), hashlib.sha256).hexdigest()


class OTPService:
    """Servicio para las integraciones externas."""
    def __init__(self) -> None:
        """Inicializa el otpservice con la configuracion necesaria."""
        self.settings = get_settings()
        self.lookup = ContractLookupClient()
        self.smtp = SMTPClient()

    async def _can_send_otp(self, recipient: str, session_id: str) -> bool:
        """Indica si otp send se cumple."""
        redis = await get_redis_client()
        return await redis.get(_key("cooldown", recipient, session_id)) is None

    async def _set_otp_cooldown(self, recipient: str, session_id: str, seconds: int) -> None:
        """Devuelve el cooldown set otp."""
        redis = await get_redis_client()
        await redis.set(_key("cooldown", recipient, session_id), "1", ex=seconds)

    async def _set_otp_cache(self, recipient: str, session_id: str, otp: str, seconds: int) -> None:
        """Devuelve el cache set otp."""
        redis = await get_redis_client()
        await redis.set(_key("cache", recipient, session_id), otp, ex=seconds)

    async def _get_otp_cache(self, recipient: str, session_id: str) -> str:
        """Devuelve otp cache."""
        redis = await get_redis_client()
        return await redis.get(_key("cache", recipient, session_id)) or ""

    async def _get_otp_cache_ttl(self, recipient: str, session_id: str) -> int:
        """Devuelve otp cache ttl."""
        redis = await get_redis_client()
        ttl = await redis.ttl(_key("cache", recipient, session_id))
        return max(0, int(ttl or 0))

    async def _reset_otp_attempts(self, recipient: str, session_id: str) -> None:
        """Reinicia otp attempts para comenzar de nuevo."""
        redis = await get_redis_client()
        await redis.delete(_key("attempts", recipient, session_id))

    async def _incr_otp_attempts(self, recipient: str, session_id: str) -> int:
        """Devuelve el attempts incr otp."""
        redis = await get_redis_client()
        val = await redis.incr(_key("attempts", recipient, session_id))
        await redis.expire(_key("attempts", recipient, session_id), self.settings.memory_ttl_seconds)
        return int(val)

    async def _unlock_otp(self, recipient: str, session_id: str) -> None:
        """Devuelve el otp unlock."""
        redis = await get_redis_client()
        await redis.delete(_key("locked", recipient, session_id))

    async def _lock_otp(self, recipient: str, session_id: str, seconds: int) -> None:
        """Devuelve el otp candado."""
        redis = await get_redis_client()
        await redis.set(_key("locked", recipient, session_id), "1", ex=seconds)

    async def _is_otp_locked(self, recipient: str, session_id: str) -> bool:
        """Indica si locked otp se cumple."""
        redis = await get_redis_client()
        return await redis.get(_key("locked", recipient, session_id)) is not None

    async def generate_otp(self, recipient: str, session_id: str, cedula: str | None) -> dict:
        """Devuelve el otp generate."""
        if not cedula:
            return {"ok": False, "error": "Falta cédula para generar OTP."}
        if not await self._can_send_otp(recipient, session_id):
            return {"ok": True, "data": {"cooldown": True, "message": "OTP ya fue enviado recientemente."}}

        email = await self.lookup.get_email_by_cedula(cedula)
        if not email:
            try:
                info = await self.lookup.info_personal_by_cedula(cedula)
                data = info.get("data") if isinstance(info, dict) else None
                if isinstance(data, list):
                    for item in data:
                        partner = item.get("partner") if isinstance(item, dict) else None
                        partner_email = (partner or {}).get("email") if isinstance(partner, dict) else ""
                        if partner_email:
                            email = str(partner_email)
                            break
            except Exception:
                logger.exception("otp_email_lookup_fallback_failed cedula=%s", cedula)

        if not email:
            return {"ok": False, "error": "No se encontró correo del titular para enviar OTP."}

        otp = _gen_otp_hex(self.settings.otp_code_len)
        subject = "Código de verificación GoNet"
        body = f"Tu código de verificación es: {otp}\n\nSi no solicitaste esto, ignora este mensaje."
        send_result = await self.smtp.send_email(email, subject, body)
        if send_result.get("status") not in {"sent", "skipped"}:
            return {"ok": False, "error": "No se pudo enviar el correo OTP."}

        if self.settings.otp_pg_dsn or self.settings.pg_dsn:
            if self.settings.otp_store_plaintext:
                await insert_otp(recipient, otp)
            else:
                otp_hash = _otp_hmac(otp, recipient, session_id, self.settings.otp_hmac_secret or "")
                await insert_otp(recipient, f"hmac${otp_hash}")

        await self._set_otp_cooldown(recipient, session_id, self.settings.otp_resend_cooldown_seconds)
        await self._reset_otp_attempts(recipient, session_id)
        await self._unlock_otp(recipient, session_id)
        await self._set_otp_cache(recipient, session_id, otp, self.settings.otp_ttl_seconds)

        return {"ok": True, "data": {"sent_to": email}}

    async def verify_otp(self, recipient: str, session_id: str, otp: str) -> dict:
        """Devuelve el otp verify."""
        if await self._is_otp_locked(recipient, session_id):
            return {"ok": False, "data": {"verified": False, "locked": True, "attempts_left": 0}, "error": "OTP bloqueado"}

        cached = await self._get_otp_cache(recipient, session_id)
        if cached:
            expected = cached
            created_at = None
        else:
            if not (self.settings.otp_pg_dsn or self.settings.pg_dsn):
                return {"ok": False, "error": "No existe OTP generado para este usuario."}
            last = await get_last_otp(recipient)
            if not last:
                return {"ok": False, "error": "No existe OTP generado para este usuario."}
            expected = str(last.get("otp") or "")
            created_at = last.get("fecha")

        if created_at:
            if isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except Exception:
                    created_at = None
            if created_at and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at and (datetime.now(timezone.utc) - created_at).total_seconds() > self.settings.otp_ttl_seconds:
                return {"ok": False, "error": "OTP expirado"}

        candidate = otp.strip().upper()
        if expected.startswith("hmac$"):
            incoming_hash = _otp_hmac(candidate, recipient, session_id, self.settings.otp_hmac_secret or "")
            verified = hmac.compare_digest(incoming_hash, expected.split("$", 1)[1])
        else:
            verified = candidate == expected.strip().upper()

        if verified:
            await self._reset_otp_attempts(recipient, session_id)
            await self._unlock_otp(recipient, session_id)
            return {"ok": True, "data": {"verified": True, "attempts_left": self.settings.otp_max_attempts}}

        attempts = await self._incr_otp_attempts(recipient, session_id)
        locked = attempts >= self.settings.otp_max_attempts
        if locked:
            await self._lock_otp(recipient, session_id, self.settings.otp_lockout_seconds)
        return {
            "ok": False,
            "data": {
                "verified": False,
                "attempts": attempts,
                "locked": locked,
                "attempts_left": max(0, self.settings.otp_max_attempts - attempts),
            },
            "error": "OTP incorrecto",
        }
