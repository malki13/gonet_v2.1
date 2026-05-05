"""programador de inactividad y cierre de sesiones."""

import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from packages.channels.delivery import ChannelDeliveryService
from packages.integrations.contact_registry import build_contact_registry
from packages.integrations.odoo_chat import OdooChatClient
from packages.orchestrator.response_composer import ResponseComposer
from packages.orchestrator.session_context import SessionContextService
from packages.shared.config import Settings, get_settings
from packages.shared.response_planner import build_clarify_response_plan, response_plan_metadata
from packages.shared.schemas import SessionState

logger = logging.getLogger("orchestrator.inactivity")

AVISO_CIERRE = (
    "Voy a cerrar este chat por inactividad. "
    "Si luego necesita algo más, escríbanos por aquí y retomamos."
)


def _utcnow() -> datetime:
    """Devuelve la hora actual en UTC."""
    return datetime.now(timezone.utc)


class InactivityService:
    """Servicio central que coordina la orquestación conversacional."""
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        sessions: SessionContextService | None = None,
        delivery: ChannelDeliveryService | None = None,
        odoo: OdooChatClient | None = None,
    ) -> None:
        """Inicializa el inactivityservice con la configuracion necesaria."""
        self.settings = settings or get_settings()
        self.sessions = sessions or SessionContextService()
        self.delivery = delivery or ChannelDeliveryService()
        self.odoo = odoo or OdooChatClient()
        self.contacts = build_contact_registry()
        self.response_composer = ResponseComposer()

    async def process_human_inactivity(self) -> int:
        """Procesa inactivity human y avanza el flujo."""
        return await self._process_inactivity(human_handoff=True)

    async def process_ia_inactivity(self) -> int:
        """Procesa inactivity ia y avanza el flujo."""
        return await self._process_inactivity(human_handoff=False)

    async def _process_inactivity(self, *, human_handoff: bool) -> int:
        """Procesa inactivity y avanza el flujo."""
        states = await self.sessions.list_sessions()
        if not states:
            return 0
        threshold = timedelta(
            minutes=self.settings.time_inactive_chat if human_handoff else self.settings.time_inactive_chat_ia
        )
        now = _utcnow()
        processed = 0
        for state in states:
            if bool(state.human_handoff) != human_handoff:
                continue
            if now - self._last_activity(state) < threshold:
                continue
            await self._close_inactive_session(state, human_handoff=human_handoff)
            processed += 1
        if processed:
            logger.info("inactive_sessions_closed count=%s human_handoff=%s", processed, human_handoff)
        return processed

    @staticmethod
    def _last_activity(state: SessionState) -> datetime:
        """Devuelve la ultima marca de actividad registrada."""
        candidates = [
            candidate
            for candidate in (state.updated_at, state.last_assistant_message_at, state.last_user_message_at)
            if candidate is not None
        ]
        if not candidates:
            return _utcnow()

        normalized: list[datetime] = []
        for candidate in candidates:
            if candidate.tzinfo is None:
                normalized.append(candidate.replace(tzinfo=timezone.utc))
            else:
                normalized.append(candidate.astimezone(timezone.utc))
        return max(normalized)

    @staticmethod
    def _infer_group(state: SessionState, settings: Settings) -> str:
        """Devuelve el grupo infer."""
        configured = str((state.metadata or {}).get("handoff_group") or "").strip().lower()
        if configured:
            return configured
        intent = (state.current_intent or "").lower()
        last_agent = (state.last_agent or "").lower()
        if intent in {"sales", "commercial", "agencies"} or last_agent == "sales":
            return "iainfo"
        return "support"

    @classmethod
    def _conversation_domain(cls, state: SessionState) -> str:
        """Devuelve el dominio de la conversacion asociado a la sesion."""
        group = cls._infer_group(state, get_settings()).lower()
        if group == "iainfo":
            return "sales"
        intent = (state.current_intent or "").lower()
        last_agent = (state.last_agent or "").lower()
        if intent.startswith("billing") or last_agent == "billing":
            return "billing"
        if intent in {"sales", "commercial", "agencies"} or last_agent == "sales":
            return "sales"
        return "support"

    @classmethod
    def _closure_message(cls, state: SessionState, *, human_handoff: bool) -> str:
        """Devuelve el mensaje closure."""
        domain = cls._conversation_domain(state)
        if human_handoff:
            if domain == "sales":
                return (
                    "Voy cerrando este chat por inactividad. "
                    "Si luego desea retomar su solicitud o volver a hablar con un asesor comercial especializado, escríbanos por aquí y seguimos."
                )
            return (
                "Voy cerrando este chat por inactividad. "
                "Si luego desea retomar el caso con un asesor especializado, escríbanos por aquí y lo continuamos."
            )
        if domain == "billing":
            return (
                "Voy cerrando este chat por inactividad. "
                "Si luego desea retomar lo de pagos, comprobantes o facturación, escríbame por aquí y seguimos."
            )
        if domain == "sales":
            return (
                "Voy cerrando este chat por inactividad. "
                "Si luego desea retomar lo de planes, cobertura o agencias, escríbame por aquí y seguimos."
            )
        if domain == "support":
            return (
                "Voy cerrando este chat por inactividad. "
                "Si luego desea retomar la revisión técnica, escríbame por aquí y seguimos."
            )
        return AVISO_CIERRE

    async def _close_inactive_session(self, state: SessionState, *, human_handoff: bool) -> None:
        """Devuelve la session close inactive."""
        if state.channel in {"whatsapp", "messenger"} and state.recipient:
            closure_message = self._closure_message(state, human_handoff=human_handoff)
            response_plan = build_clarify_response_plan(
                message=closure_message,
                conversation_state=f"session_closed_{self._conversation_domain(state)}",
                reply_goal="cerrar la conversación con calidez y dejar claro cómo retomarla",
                hypothesis="inactive_chat_timeout",
                next_step="wait_for_customer_reopen",
                evidence=[
                    f"Handoff activo: {'si' if human_handoff else 'no'}",
                    f"Intento actual: {state.current_intent}" if state.current_intent else None,
                    f"Último agente: {state.last_agent}" if state.last_agent else None,
                ],
            )
            rendered_message = closure_message
            try:
                rendered = await self.response_composer.compose_direct_result(
                    state=state,
                    channel=state.channel,
                    recipient=state.recipient,
                    raw_message=closure_message,
                    intent="session_closed",
                    result_agent="clarify",
                    decision_agent="clarify",
                    reason="inactivity_close",
                    metadata=response_plan_metadata(
                        response_plan,
                        {"human_handoff": human_handoff},
                    ),
                )
                rendered_message = rendered.message
            except Exception:
                logger.exception("inactivity_compose_failed session_id=%s", state.session_id)
            try:
                await self.delivery.deliver(
                    channel=state.channel,
                    recipient=state.recipient,
                    message=rendered_message,
                )
            except Exception:
                logger.exception("inactivity_user_notification_failed session_id=%s", state.session_id)

        if human_handoff and state.channel in {"whatsapp", "messenger"} and state.recipient:
            try:
                await self.odoo.notify_channel_closed(
                    channel=state.channel,
                    recipient=state.recipient,
                    cedula=state.cedula,
                    group=self._infer_group(state, self.settings),
                    origen="gonet",
                )
            except Exception:
                logger.exception("inactivity_odoo_notification_failed session_id=%s", state.session_id)

        await self.contacts.close_contact(recipient=state.recipient, red=state.channel)
        await self.sessions.clear(session_id=state.session_id, recipient=state.recipient, channel=state.channel)


class InactivityScheduler:
    """Programador de inactividad que cierra sesiones y emite mensajes de cierre."""
    def __init__(self, *, settings: Settings | None = None, service: InactivityService | None = None) -> None:
        """Inicializa el programador de inactividad y cierre de sesiones con la configuracion necesaria."""
        self.settings = settings or get_settings()
        self.service = service or InactivityService(settings=self.settings)
        self._scheduler: AsyncIOScheduler | None = None

    async def start(self) -> None:
        """Inicia el servicio."""
        if not self.settings.enable_inactivity_scheduler:
            logger.info("inactivity_scheduler_disabled")
            return
        if self._scheduler and self._scheduler.running:
            return
        self._scheduler = AsyncIOScheduler(timezone=ZoneInfo("America/Guayaquil"))
        self._scheduler.add_job(
            self.service.process_human_inactivity,
            "interval",
            minutes=self.settings.send_inactive_chat,
            id="inactivity_human",
            max_instances=1,
            replace_existing=True,
            misfire_grace_time=30,
        )
        self._scheduler.add_job(
            self.service.process_ia_inactivity,
            "interval",
            minutes=self.settings.update_inactive_chat,
            id="inactivity_ia",
            max_instances=1,
            replace_existing=True,
            misfire_grace_time=30,
        )
        self._scheduler.start()
        logger.info(
            "inactivity_scheduler_started human_every=%s ia_every=%s",
            self.settings.send_inactive_chat,
            self.settings.update_inactive_chat,
        )

    async def shutdown(self) -> None:
        """Detiene el servicio y libera recursos."""
        if not self._scheduler:
            return
        with suppress(Exception):
            self._scheduler.shutdown(wait=False)
        logger.info("inactivity_scheduler_stopped")
