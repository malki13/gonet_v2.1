"""Acceso a la sesion conversacional compartida."""

from datetime import datetime, timezone

from packages.integrations.redis_store import build_session_store
from packages.shared.schemas import InboundMessage, SessionState


class SessionContextService:
    """el servicio de contexto de sesión"""
    def __init__(self):
        """Inicializa el sessioncontextservice con la configuracion necesaria."""
        self.store = build_session_store()

    async def load(self, message: InboundMessage) -> SessionState:
        """Carga el estado persistido antes de continuar."""
        state = await self.store.get(message.session_id)
        if state is not None:
            same_recipient = state.recipient == message.recipient
            same_channel = str(state.channel or "").strip().lower() == str(message.channel or "").strip().lower()
            if not (same_recipient and same_channel):
                state = None
        if state is None:
            state = SessionState(
                session_id=message.session_id,
                channel=message.channel,
                recipient=message.recipient,
                cedula=message.cedula,
            )
        if message.cedula and not state.cedula:
            state.cedula = message.cedula
        return state

    async def save(self, state: SessionState) -> None:
        """Guarda el estado para reutilizarlo despues."""
        await self.store.set(state)

    async def list_sessions(self) -> list[SessionState]:
        """Lista Carga, guarda y expira el estado conversacional de cada sesión."""
        list_sessions = getattr(self.store, "list_sessions", None)
        if list_sessions is None:
            return []
        return await list_sessions()

    async def touch(
        self,
        *,
        session_id: str | None = None,
        recipient: str | None = None,
        channel: str | None = None,
        actor: str = "assistant",
        human_handoff: bool | None = None,
    ) -> SessionState | None:
        """Actualiza la marca de actividad de la sesion."""
        state = None
        if session_id:
            state = await self.store.get(session_id)
        if state is None and recipient:
            get_by_recipient = getattr(self.store, "get_by_recipient", None)
            if get_by_recipient is not None:
                state = await get_by_recipient(recipient, channel=channel)
        if state is None:
            return None
        now = datetime.now(timezone.utc)
        state.updated_at = now
        if actor == "user":
            state.last_user_message_at = now
        else:
            state.last_assistant_message_at = now
        if human_handoff is not None:
            state.human_handoff = human_handoff
        await self.store.set(state)
        return state

    async def clear(self, *, session_id: str | None = None, recipient: str | None = None, channel: str | None = None) -> bool:
        """Limpia el estado asociado."""
        delete = getattr(self.store, "delete", None)
        if delete is None:
            return False
        return await delete(session_id=session_id, recipient=recipient, channel=channel)
