"""Outbox de desarrollo para inspeccionar mensajes salientes."""

import json
import logging
from dataclasses import dataclass

from packages.shared.config import get_settings
from packages.shared.schemas import OutboundDelivery

logger = logging.getLogger("outbox_store")

try:
    import redis.asyncio as redis_asyncio
except ImportError:  # pragma: no cover
    redis_asyncio = None


@dataclass
class InMemoryOutboxStore:
    """Almacena y recupera datos de las integraciones externas."""
    _messages: dict[str, list[dict]]

    async def add(self, delivery: OutboundDelivery) -> None:
        """Agrega el elemento si todavia no existe."""
        key = delivery.session_id or delivery.recipient
        items = self._messages.setdefault(key, [])
        items.append(delivery.model_dump())
        if len(items) > 50:
            del items[:-50]

    async def list_messages(self, *, session_id: str | None = None, recipient: str | None = None) -> list[OutboundDelivery]:
        """Lista Almacena y recupera datos de las integraciones externas."""
        key = session_id or recipient
        if not key:
            return []
        return [OutboundDelivery(**item) for item in self._messages.get(key, [])]


class RedisOutboxStore:
    """Almacena y recupera datos de las integraciones externas."""
    def __init__(self, url: str):
        """Inicializa el redisoutboxstore con la configuracion necesaria."""
        self._url = url
        self._client = None
        self._fallback = InMemoryOutboxStore({})

    async def _client_or_none(self):
        """Devuelve el none client or."""
        if redis_asyncio is None:
            return None
        if self._client is not None:
            return self._client
        try:
            self._client = redis_asyncio.from_url(self._url, encoding="utf-8", decode_responses=True)
            await self._client.ping()
            return self._client
        except Exception:
            logger.exception("outbox_redis_unavailable; using_in_memory_fallback")
            self._client = None
            return None

    @staticmethod
    def _key(*, session_id: str | None = None, recipient: str | None = None) -> str | None:
        """Devuelve la clave usada para agrupar o persistir datos."""
        if session_id:
            return f"outbox:session:{session_id}"
        if recipient:
            return f"outbox:recipient:{recipient}"
        return None

    async def add(self, delivery: OutboundDelivery) -> None:
        """Agrega el elemento si todavia no existe."""
        client = await self._client_or_none()
        if client is None:
            await self._fallback.add(delivery)
            return
        key = self._key(session_id=delivery.session_id, recipient=delivery.recipient)
        if key is None:
            await self._fallback.add(delivery)
            return
        await client.rpush(key, json.dumps(delivery.model_dump()))
        await client.ltrim(key, -50, -1)

    async def list_messages(self, *, session_id: str | None = None, recipient: str | None = None) -> list[OutboundDelivery]:
        """Lista Almacena y recupera datos de las integraciones externas."""
        client = await self._client_or_none()
        if client is None:
            return await self._fallback.list_messages(session_id=session_id, recipient=recipient)
        key = self._key(session_id=session_id, recipient=recipient)
        if key is None:
            return []
        items = await client.lrange(key, 0, -1)
        return [OutboundDelivery(**json.loads(item)) for item in items]


def build_outbox_store() -> RedisOutboxStore | InMemoryOutboxStore:
    """Construye store bandeja de salida a partir del contexto disponible."""
    settings = get_settings()
    if settings.redis_url:
        return RedisOutboxStore(settings.redis_url)
    return InMemoryOutboxStore({})
