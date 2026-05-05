"""Store compartido de sesiones sobre Redis con fallback en memoria."""

import json
import logging
from functools import lru_cache
from dataclasses import dataclass

from packages.shared.config import get_settings
from packages.shared.errors import SessionStoreError
from packages.shared.schemas import SessionState

logger = logging.getLogger("redis_store")
SESSION_INDEX_KEY = "session:index"


def _party_key(channel: str | None, recipient: str | None) -> str | None:
    """Devuelve el clave party."""
    normalized_channel = str(channel or "").strip().lower()
    normalized_recipient = str(recipient or "").strip()
    if not normalized_channel or not normalized_recipient:
        return None
    return f"{normalized_channel}:{normalized_recipient}"

try:
    import redis.asyncio as redis_asyncio
except ImportError:  # pragma: no cover
    redis_asyncio = None


@dataclass
class InMemorySessionStore:
    """Almacena y recupera datos de las integraciones externas."""
    _state: dict[str, dict]
    _recipient_index: dict[str, str]
    _party_index: dict[str, str]

    async def get(self, session_id: str) -> SessionState | None:
        """Devuelve el get."""
        raw = self._state.get(session_id)
        return SessionState(**raw) if raw else None

    async def set(self, session: SessionState) -> None:
        """Devuelve el set."""
        self._state[session.session_id] = session.model_dump(mode="json")
        if session.recipient:
            self._recipient_index[session.recipient] = session.session_id
        party_key = _party_key(session.channel, session.recipient)
        if party_key:
            self._party_index[party_key] = session.session_id

    async def get_by_recipient(self, recipient: str, channel: str | None = None) -> SessionState | None:
        """Devuelve by recipient."""
        session_id = None
        party_key = _party_key(channel, recipient)
        if party_key:
            session_id = self._party_index.get(party_key)
        if not session_id:
            session_id = self._recipient_index.get(recipient)
        if not session_id:
            return None
        return await self.get(session_id)

    async def list_sessions(self) -> list[SessionState]:
        """Lista Almacena y recupera datos de las integraciones externas."""
        return [SessionState(**raw) for raw in self._state.values()]

    async def delete(self, *, session_id: str | None = None, recipient: str | None = None, channel: str | None = None) -> bool:
        """Elimina la entrada indicada del almacen."""
        target_session_id = session_id
        if not target_session_id and recipient:
            party_key = _party_key(channel, recipient)
            if party_key:
                target_session_id = self._party_index.get(party_key)
            if not target_session_id:
                target_session_id = self._recipient_index.get(recipient)
        if not target_session_id:
            return False
        raw = self._state.pop(target_session_id, None)
        if raw:
            stored_recipient = str(raw.get("recipient") or "").strip()
            stored_channel = str(raw.get("channel") or "").strip().lower() or None
            if stored_recipient:
                self._recipient_index.pop(stored_recipient, None)
            stored_party_key = _party_key(stored_channel, stored_recipient)
            if stored_party_key:
                self._party_index.pop(stored_party_key, None)
            if recipient:
                self._recipient_index.pop(recipient, None)
                requested_party_key = _party_key(channel, recipient)
                if requested_party_key:
                    self._party_index.pop(requested_party_key, None)
            return True
        if recipient:
            self._recipient_index.pop(recipient, None)
            requested_party_key = _party_key(channel, recipient)
            if requested_party_key:
                self._party_index.pop(requested_party_key, None)
        return False


class RedisSessionStore:
    """Almacena y recupera datos de las integraciones externas."""
    def __init__(self, url: str, ttl_seconds: int):
        """Inicializa el redissessionstore con la configuracion necesaria."""
        self._url = url
        self._ttl_seconds = ttl_seconds
        self._client = None

    async def _client_or_raise(self):
        """Devuelve el raise client or."""
        if redis_asyncio is None:
            raise SessionStoreError("redis_asyncio_not_available")
        if self._client is not None:
            return self._client
        try:
            self._client = redis_asyncio.from_url(self._url, encoding="utf-8", decode_responses=True)
            await self._client.ping()
            return self._client
        except Exception as exc:
            self._client = None
            logger.exception("redis_session_store_unavailable url=%s", self._url)
            raise SessionStoreError("shared_session_store_unavailable") from exc

    async def _run(self, operation: str, action):
        """Arranca el flujo principal con la configuracion cargada."""
        try:
            return await action
        except SessionStoreError:
            raise
        except Exception as exc:
            self._client = None
            logger.exception("redis_session_store_operation_failed operation=%s", operation)
            raise SessionStoreError("shared_session_store_unavailable") from exc

    async def get(self, session_id: str) -> SessionState | None:
        """Devuelve el get."""
        client = await self._client_or_raise()
        raw = await self._run("get", client.get(f"session:{session_id}"))
        return SessionState(**json.loads(raw)) if raw else None

    async def get_by_recipient(self, recipient: str, channel: str | None = None) -> SessionState | None:
        """Devuelve by recipient."""
        client = await self._client_or_raise()
        session_id = None
        party_key = _party_key(channel, recipient)
        if party_key:
            session_id = await self._run("get_by_party", client.get(f"session:recipient:{party_key}"))
        if not session_id:
            session_id = await self._run("get_by_recipient", client.get(f"session:recipient:{recipient}"))
        if not session_id:
            return None
        return await self.get(session_id)

    async def set(self, session: SessionState) -> None:
        """Devuelve el set."""
        client = await self._client_or_raise()
        payload = json.dumps(session.model_dump(mode="json"))
        await self._run("set_session", client.set(f"session:{session.session_id}", payload, ex=self._ttl_seconds))
        if session.recipient:
            await self._run(
                "set_recipient_index",
                client.set(f"session:recipient:{session.recipient}", session.session_id, ex=self._ttl_seconds),
            )
        party_key = _party_key(session.channel, session.recipient)
        if party_key:
            await self._run(
                "set_party_index",
                client.set(f"session:recipient:{party_key}", session.session_id, ex=self._ttl_seconds),
            )
        await self._run("add_session_index", client.sadd(SESSION_INDEX_KEY, session.session_id))

    async def list_sessions(self) -> list[SessionState]:
        """Lista Almacena y recupera datos de las integraciones externas."""
        client = await self._client_or_raise()
        session_ids = await self._run("list_session_ids", client.smembers(SESSION_INDEX_KEY))
        if not session_ids:
            return []
        sessions: list[SessionState] = []
        stale: list[str] = []
        for session_id in session_ids:
            raw = await self._run("list_session_get", client.get(f"session:{session_id}"))
            if not raw:
                stale.append(session_id)
                continue
            sessions.append(SessionState(**json.loads(raw)))
        if stale:
            await self._run("list_session_cleanup", client.srem(SESSION_INDEX_KEY, *stale))
        return sessions

    async def delete(self, *, session_id: str | None = None, recipient: str | None = None, channel: str | None = None) -> bool:
        """Elimina la entrada indicada del almacen."""
        client = await self._client_or_raise()
        target_session_id = session_id
        if not target_session_id and recipient:
            party_key = _party_key(channel, recipient)
            if party_key:
                target_session_id = await self._run("delete_lookup_party", client.get(f"session:recipient:{party_key}"))
            if not target_session_id:
                target_session_id = await self._run("delete_lookup_recipient", client.get(f"session:recipient:{recipient}"))
        if not target_session_id:
            return False
        raw = await self._run("delete_get_session", client.get(f"session:{target_session_id}"))
        stored_recipient = None
        stored_channel = None
        if raw:
            try:
                payload = json.loads(raw) or {}
                stored_recipient = str(payload.get("recipient") or "").strip() or None
                stored_channel = str(payload.get("channel") or "").strip().lower() or None
            except Exception:
                stored_recipient = None
                stored_channel = None
        deleted = bool(await self._run("delete_session", client.delete(f"session:{target_session_id}")))
        if recipient:
            await self._run("delete_recipient_index", client.delete(f"session:recipient:{recipient}"))
            requested_party_key = _party_key(channel, recipient)
            if requested_party_key:
                await self._run("delete_requested_party_index", client.delete(f"session:recipient:{requested_party_key}"))
        if stored_recipient:
            await self._run("delete_stored_recipient_index", client.delete(f"session:recipient:{stored_recipient}"))
        stored_party_key = _party_key(stored_channel, stored_recipient)
        if stored_party_key:
            await self._run("delete_stored_party_index", client.delete(f"session:recipient:{stored_party_key}"))
        await self._run("delete_session_index", client.srem(SESSION_INDEX_KEY, target_session_id))
        return deleted


@lru_cache(maxsize=1)
def build_session_store() -> RedisSessionStore | InMemorySessionStore:
    """Construye store session a partir del contexto disponible."""
    settings = get_settings()
    if settings.redis_url:
        return RedisSessionStore(settings.redis_url, settings.memory_ttl_seconds)
    return InMemorySessionStore({}, {}, {})
