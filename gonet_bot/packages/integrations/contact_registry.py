"""Registro compartido de contactos y asignaciones operativas."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from packages.shared.config import get_settings

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None

if TYPE_CHECKING:
    import asyncpg as asyncpg_module

    AsyncPGPool = asyncpg_module.Pool
    AsyncPGRecord = asyncpg_module.Record
else:  # pragma: no cover
    AsyncPGPool = Any
    AsyncPGRecord = Any

logger = logging.getLogger("integrations.contact_registry")
ECUADOR_TZ = ZoneInfo("America/Guayaquil")


def _now_ecuador() -> datetime:
    """Devuelve el ecuador now."""
    return datetime.now(ECUADOR_TZ)


def _normalize_dsn(dsn: str) -> str:
    """Normaliza dsn."""
    normalized = str(dsn or "").strip()
    if not normalized:
        return normalized
    normalized = normalized.replace("postgresql+asyncpg://", "postgresql://", 1)
    normalized = normalized.replace("postgres+asyncpg://", "postgres://", 1)
    parts = urlsplit(normalized)
    if not parts.query:
        return normalized
    filtered_query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        lowered_value = value.lower()
        if lowered in {"ssl", "sslmode"} and lowered_value in {"disable", "false", "0", "off"}:
            continue
        filtered_query.append((key, value))
    return urlunsplit(parts._replace(query=urlencode(filtered_query)))


@dataclass
class NullContactRegistry:
    """Registro de presencia y contexto de los contactos.."""
    async def resolve_session(self, **_: Any) -> dict:
        """Resuelve session."""
        return {"status": "skipped", "exists": False, "session_id": None, "valido": False}

    async def mark_consent_accepted(self, **_: Any) -> dict:
        """Marca consent accepted con la información confirmada."""
        return {"status": "skipped", "reason": "missing_contact_registry_dsn"}

    async def touch_contact(self, **_: Any) -> dict:
        """Actualiza contact."""
        return {"status": "skipped", "reason": "missing_contact_registry_dsn"}

    async def mark_ai_active(self, **_: Any) -> dict:
        """Marca ai active con la información confirmada."""
        return {"status": "skipped", "reason": "missing_contact_registry_dsn"}

    async def mark_human_active(self, **_: Any) -> dict:
        """Marca human active con la información confirmada."""
        return {"status": "skipped", "reason": "missing_contact_registry_dsn"}

    async def assign_odoo_channel(self, **_: Any) -> dict:
        """Devuelve el channel assign odoo."""
        return {"status": "skipped", "reason": "missing_contact_registry_dsn"}

    async def close_contact(self, **_: Any) -> dict:
        """Devuelve el contact close."""
        return {"status": "skipped", "reason": "missing_contact_registry_dsn"}

    async def close_by_channel_id(self, *_: Any, **__: Any) -> dict:
        """Devuelve el id close by channel."""
        return {"status": "skipped", "reason": "missing_contact_registry_dsn"}


class AsyncPGContactRegistry:
    """Registro de presencia y contexto de los contactos.."""
    def __init__(self, dsn: str) -> None:
        """Inicializa el asyncpgcontactregistry con la configuracion necesaria."""
        self._dsn = _normalize_dsn(dsn)
        self._pool: AsyncPGPool | None = None
        self._schema_ready = False

    async def _pool_or_none(self) -> AsyncPGPool | None:
        """Devuelve el none pool or."""
        if asyncpg is None:
            return None
        if self._pool is not None:
            return self._pool
        try:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4, command_timeout=20)
        except Exception:
            logger.exception("contact_registry_unavailable dsn=%s", self._dsn)
            self._pool = None
        return self._pool

    async def _ensure_schema(self) -> bool:
        """Devuelve la schema ensure."""
        if self._schema_ready:
            return True
        pool = await self._pool_or_none()
        if pool is None:
            return False
        ddl = """
        CREATE TABLE IF NOT EXISTS usuarios_gonet (
            id SERIAL PRIMARY KEY,
            identificacion VARCHAR(20),
            recipient VARCHAR(50) NOT NULL,
            red VARCHAR(20) NOT NULL,
            activo BOOLEAN DEFAULT FALSE,
            internal_user INTEGER,
            channel_id INTEGER,
            active_date TIMESTAMPTZ NOT NULL,
            session_id VARCHAR(30),
            chat_ia TEXT,
            activo_ia BOOLEAN DEFAULT FALSE,
            grupo VARCHAR(50),
            menu_id VARCHAR(10) NOT NULL DEFAULT '1',
            bot VARCHAR(200) NOT NULL DEFAULT 'gonet-platform',
            calificacion VARCHAR(30),
            otp VARCHAR(30),
            valido BOOLEAN DEFAULT FALSE
        );
        """
        try:
            async with pool.acquire() as conn:
                await conn.execute(ddl)
        except Exception:
            logger.exception("contact_registry_schema_failed")
            return False
        self._schema_ready = True
        return True

    @staticmethod
    def _enabled(channel: str | None, recipient: str | None) -> bool:
        """Indica si la integracion esta habilitada por configuracion."""
        return str(channel or "").strip().lower() in {"whatsapp", "messenger"} and bool(str(recipient or "").strip())

    async def _fetch_contact_row(
        self,
        *,
        recipient: str | None = None,
        red: str | None = None,
        channel_id: int | None = None,
    ) -> AsyncPGRecord | None:
        """Devuelve la fila fetch contact."""
        if not await self._ensure_schema():
            return None
        pool = await self._pool_or_none()
        if pool is None:
            return None
        async with pool.acquire() as conn:
            if channel_id is not None:
                return await conn.fetchrow(
                    """
                    SELECT id, recipient, red, session_id, internal_user, channel_id, grupo, activo, activo_ia, identificacion
                    FROM usuarios_gonet
                    WHERE channel_id = $1
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    channel_id,
                )
            if not recipient or not red:
                return None
            return await conn.fetchrow(
                """
                SELECT id, recipient, red, session_id, internal_user, channel_id, grupo, activo, activo_ia, identificacion
                FROM usuarios_gonet
                WHERE recipient = $1 AND red = $2
                ORDER BY id DESC
                LIMIT 1
                """,
                recipient,
                red,
            )

    async def touch_contact(
        self,
        *,
        recipient: str,
        red: str,
        identificacion: str | None = None,
        session_id: str | None = None,
        menu_id: str = "1",
        bot: str = "gonet-platform",
        chat_preview: str | None = None,
    ) -> dict:
        """Actualiza contact."""
        if not self._enabled(red, recipient):
            return {"status": "skipped", "reason": "unsupported_channel"}
        row = await self._fetch_contact_row(recipient=recipient, red=red)
        if row is None and not await self._ensure_schema():
            return {"status": "skipped", "reason": "registry_unavailable"}
        pool = await self._pool_or_none()
        if pool is None:
            return {"status": "skipped", "reason": "registry_unavailable"}
        now = _now_ecuador()
        async with pool.acquire() as conn:
            if row is None:
                effective_session = (session_id or uuid.uuid4().hex[:12])[:30]
                await conn.execute(
                    """
                    INSERT INTO usuarios_gonet
                    (identificacion, recipient, red, activo, active_date, session_id, chat_ia, activo_ia, grupo, menu_id, bot, valido)
                    VALUES ($1, $2, $3, FALSE, $4, $5, $6, FALSE, NULL, $7, $8, FALSE)
                    """,
                    identificacion,
                    recipient,
                    red,
                    now,
                    effective_session,
                    chat_preview,
                    menu_id,
                    bot,
                )
                logger.info("contact_registry_insert recipient=%s red=%s session_id=%s", recipient, red, effective_session)
                return {"status": "inserted", "recipient": recipient, "red": red, "session_id": effective_session}
            await conn.execute(
                """
                UPDATE usuarios_gonet
                SET identificacion = COALESCE($1, identificacion),
                    active_date = $2,
                    session_id = COALESCE($3, session_id),
                    chat_ia = COALESCE($4, chat_ia),
                    bot = $5,
                    valido = FALSE
                WHERE id = $6
                """,
                identificacion,
                now,
                session_id,
                chat_preview,
                bot,
                row["id"],
            )
        logger.info("contact_registry_touch recipient=%s red=%s session_id=%s", recipient, red, session_id)
        return {"status": "updated", "recipient": recipient, "red": red, "session_id": session_id or row["session_id"]}

    async def resolve_session(self, *, recipient: str, red: str) -> dict:
        """Resuelve session."""
        if not self._enabled(red, recipient):
            return {"status": "skipped", "exists": False, "session_id": None}
        row = await self._fetch_contact_row(recipient=recipient, red=red)
        if row is None:
            return {"status": "missing", "exists": False, "session_id": None}
        return {
            "status": "found",
            "exists": True,
            "session_id": str(row["session_id"] or "").strip() or None,
            "internal_user": row["internal_user"],
            "channel_id": row["channel_id"],
            "group": row["grupo"],
            "human_active": bool(row["activo"]),
            "ai_active": bool(row["activo_ia"]),
            "cedula": str(row["identificacion"] or "").strip() or None,
        }

    async def mark_ai_active(
        self,
        *,
        recipient: str,
        red: str,
        identificacion: str | None = None,
        session_id: str | None = None,
        group: str | None = None,
        menu_id: str = "4",
        bot: str = "gonet-platform",
        chat_preview: str | None = None,
    ) -> dict:
        """Marca ai active con la información confirmada."""
        if not self._enabled(red, recipient):
            return {"status": "skipped", "reason": "unsupported_channel"}
        await self.touch_contact(
            recipient=recipient,
            red=red,
            identificacion=identificacion,
            session_id=session_id,
            menu_id=menu_id,
            bot=bot,
            chat_preview=chat_preview,
        )
        row = await self._fetch_contact_row(recipient=recipient, red=red)
        if row is None:
            return {"status": "skipped", "reason": "registry_unavailable"}
        pool = await self._pool_or_none()
        if pool is None:
            return {"status": "skipped", "reason": "registry_unavailable"}
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE usuarios_gonet
                SET activo = FALSE,
                    activo_ia = TRUE,
                    active_date = $1,
                    session_id = COALESCE($2, session_id),
                    identificacion = COALESCE($3, identificacion),
                    menu_id = $4,
                    bot = $5,
                    chat_ia = COALESCE($6, chat_ia),
                    grupo = COALESCE($7, grupo),
                    valido = FALSE
                WHERE id = $8
                """,
                _now_ecuador(),
                session_id,
                identificacion,
                menu_id,
                bot,
                chat_preview,
                group,
                row["id"],
            )
        logger.info("contact_registry_ai_active recipient=%s red=%s group=%s", recipient, red, group)
        return {"status": "updated", "recipient": recipient, "red": red, "mode": "ia"}

    async def mark_human_active(
        self,
        *,
        recipient: str,
        red: str,
        identificacion: str | None = None,
        session_id: str | None = None,
        group: str | None = None,
        menu_id: str = "5",
        bot: str = "gonet-platform",
        chat_preview: str | None = None,
    ) -> dict:
        """Marca human active con la información confirmada."""
        if not self._enabled(red, recipient):
            return {"status": "skipped", "reason": "unsupported_channel"}
        await self.touch_contact(
            recipient=recipient,
            red=red,
            identificacion=identificacion,
            session_id=session_id,
            menu_id=menu_id,
            bot=bot,
            chat_preview=chat_preview,
        )
        row = await self._fetch_contact_row(recipient=recipient, red=red)
        if row is None:
            return {"status": "skipped", "reason": "registry_unavailable"}
        pool = await self._pool_or_none()
        if pool is None:
            return {"status": "skipped", "reason": "registry_unavailable"}
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE usuarios_gonet
                SET activo = TRUE,
                    activo_ia = FALSE,
                    active_date = $1,
                    session_id = COALESCE($2, session_id),
                    identificacion = COALESCE($3, identificacion),
                    menu_id = $4,
                    bot = $5,
                    chat_ia = COALESCE($6, chat_ia),
                    grupo = COALESCE($7, grupo)
                WHERE id = $8
                """,
                _now_ecuador(),
                session_id,
                identificacion,
                menu_id,
                bot,
                chat_preview,
                group,
                row["id"],
            )
        logger.info("contact_registry_human_active recipient=%s red=%s group=%s", recipient, red, group)
        return {"status": "updated", "recipient": recipient, "red": red, "mode": "human"}

    async def assign_odoo_channel(
        self,
        *,
        recipient: str,
        red: str,
        internal_user: int | str | None,
        channel_id: int | str | None,
    ) -> dict:
        """Devuelve el channel assign odoo."""
        if not self._enabled(red, recipient):
            return {"status": "skipped", "reason": "unsupported_channel"}
        row = await self._fetch_contact_row(recipient=recipient, red=red)
        if row is None:
            await self.touch_contact(recipient=recipient, red=red)
            row = await self._fetch_contact_row(recipient=recipient, red=red)
        if row is None:
            return {"status": "skipped", "reason": "registry_unavailable"}
        pool = await self._pool_or_none()
        if pool is None:
            return {"status": "skipped", "reason": "registry_unavailable"}
        parsed_internal_user = int(str(internal_user).strip()) if str(internal_user or "").strip() else None
        parsed_channel_id = int(str(channel_id).strip()) if str(channel_id or "").strip() else None
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE usuarios_gonet
                SET internal_user = COALESCE($1, internal_user),
                    channel_id = COALESCE($2, channel_id),
                    active_date = $3,
                    valido = TRUE
                WHERE id = $4
                """,
                parsed_internal_user,
                parsed_channel_id,
                _now_ecuador(),
                row["id"],
            )
        logger.info(
            "contact_registry_assignment recipient=%s red=%s internal_user=%s channel_id=%s",
            recipient,
            red,
            parsed_internal_user,
            parsed_channel_id,
        )
        return {
            "status": "updated",
            "recipient": recipient,
            "red": red,
            "internal_user": parsed_internal_user,
            "channel_id": parsed_channel_id,
        }

    async def close_contact(
        self,
        *,
        recipient: str | None = None,
        red: str | None = None,
        channel_id: int | None = None,
    ) -> dict:
        """Devuelve el contact close."""
        row = await self._fetch_contact_row(recipient=recipient, red=red, channel_id=channel_id)
        if row is None:
            return {"status": "not_found", "recipient": recipient, "red": red, "channel_id": channel_id}
        pool = await self._pool_or_none()
        if pool is None:
            return {"status": "skipped", "reason": "registry_unavailable"}
        new_session_id = uuid.uuid4().hex[:12]
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE usuarios_gonet
                SET activo = FALSE,
                    activo_ia = FALSE,
                    active_date = $1,
                    session_id = $2,
                    internal_user = NULL,
                    channel_id = NULL,
                    menu_id = '1',
                    otp = NULL,
                    valido = FALSE
                WHERE id = $3
                """,
                _now_ecuador(),
                new_session_id,
                row["id"],
            )
        logger.info(
            "contact_registry_closed recipient=%s red=%s channel_id=%s new_session_id=%s",
            row["recipient"],
            row["red"],
            row["channel_id"],
            new_session_id,
        )
        return {
            "status": "closed",
            "recipient": row["recipient"],
            "red": row["red"],
            "session_id": new_session_id,
        }

    async def close_by_channel_id(self, channel_id: int | str | None) -> dict:
        """Devuelve el id close by channel."""
        parsed_channel_id = int(str(channel_id).strip()) if str(channel_id or "").strip() else None
        if parsed_channel_id is None:
            return {"status": "not_found", "channel_id": channel_id}
        return await self.close_contact(channel_id=parsed_channel_id)


@lru_cache(maxsize=1)
def build_contact_registry() -> AsyncPGContactRegistry | NullContactRegistry:
    """Construye registro de contactos a partir del contexto disponible."""
    settings = get_settings()
    dsn = settings.contact_registry_dsn
    if dsn:
        return AsyncPGContactRegistry(dsn)
    return NullContactRegistry()
