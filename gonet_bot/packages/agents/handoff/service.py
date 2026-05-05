"""Agente responsable de derivar casos a Odoo Chat."""

import logging

import httpx

from packages.integrations.odoo_chat import OdooChatClient
from packages.shared.config import get_settings
from packages.shared.response_planner import build_handoff_response_plan, response_plan_metadata
from packages.shared.schemas import AgentResult, InboundMessage, SessionState

logger = logging.getLogger("agents.handoff")


async def create_direct_handoff_result(
    *,
    odoo: OdooChatClient,
    message: InboundMessage,
    state: SessionState,
    summary: str,
    group: str,
    origen: str,
    final_message: str,
    hypothesis: str,
    reply_goal: str = "confirmar con claridad que el caso ya quedó derivado a un asesor especializado",
    next_step: str = "wait_human_followup",
    failure_message: str | None = None,
    failure_conversation_state: str = "handoff_failed",
    failure_hypothesis: str = "handoff_failed",
    failure_next_step: str = "retry_later",
    error_type: str | None = None,
) -> AgentResult:
    """Crea un handoff directo y devuelve el resultado serializable para el orquestador."""
    try:
        handoff_result = await odoo.escalate_new_client(
            channel=message.channel,
            recipient=message.recipient,
            summary=summary,
            cedula=message.cedula or state.cedula,
            origen=origen,
            group=group,
        )
        if isinstance(handoff_result, dict) and str(handoff_result.get("status") or "").strip().lower() == "skipped":
            raise RuntimeError(str(handoff_result.get("reason") or "handoff_skipped").strip() or "handoff_skipped")
    except httpx.ReadTimeout:
        logger.warning(
            "direct_handoff_timeout session_id=%s recipient=%s group=%s",
            message.session_id,
            message.recipient,
            group,
        )
        response_plan = build_handoff_response_plan(
            message=final_message,
            conversation_state="handoff_created",
            reply_goal=reply_goal,
            summary=summary,
            target_group=group,
            hypothesis=hypothesis,
            next_step=next_step,
            should_handoff=True,
            handoff_reason=summary,
        )
        timeout_metadata = {
            "summary": summary,
            "handoff_group": group,
            "handoff_origen": origen,
            "handoff_timeout": True,
        }
        if error_type:
            timeout_metadata["error_type"] = error_type
        return AgentResult(
            message=final_message,
            intent="human_handoff",
            agent="handoff",
            metadata=response_plan_metadata(response_plan, timeout_metadata),
        )
    except Exception as exc:
        logger.exception(
            "direct_handoff_failed session_id=%s recipient=%s group=%s",
            message.session_id,
            message.recipient,
            group,
        )
        failed_message = failure_message or (
            "Tuve un inconveniente interno y ahora mismo no pude dejar su caso con un asesor especializado. "
            "Por favor, vuelva a escribir en unos minutos."
        )
        response_plan = build_handoff_response_plan(
            message=failed_message,
            conversation_state=failure_conversation_state,
            reply_goal="ser honesto cuando no fue posible completar la derivación",
            summary=summary,
            target_group=group,
            hypothesis=failure_hypothesis,
            next_step=failure_next_step,
            should_handoff=False,
            handoff_reason=summary,
        )
        metadata = {
            "summary": summary,
            "handoff_group": group,
            "handoff_origen": origen,
            "handoff_failed": True,
            "handoff_error": f"{exc.__class__.__name__}: {exc}",
        }
        if error_type:
            metadata["error_type"] = error_type
        return AgentResult(
            message=failed_message,
            intent="clarify",
            agent="clarify",
            metadata=response_plan_metadata(response_plan, metadata),
        )

    if message.attachments:
        try:
            await odoo.relay_attachments(
                channel=message.channel,
                recipient=message.recipient,
                attachments=[item.model_dump() if hasattr(item, "model_dump") else item for item in (message.attachments or [])],
                cedula=message.cedula or state.cedula,
                origen=origen,
                group=group,
                internal_user=handoff_result.get("internal_user") if isinstance(handoff_result, dict) else None,
                channel_id=handoff_result.get("channel_id") if isinstance(handoff_result, dict) else None,
            )
        except Exception:
            logger.exception(
                "direct_handoff_attachment_relay_failed session_id=%s recipient=%s group=%s",
                message.session_id,
                message.recipient,
                group,
            )

    response_plan = build_handoff_response_plan(
        message=final_message,
        conversation_state="handoff_created",
        reply_goal=reply_goal,
        summary=summary,
        target_group=group,
        hypothesis=hypothesis,
        next_step=next_step,
        should_handoff=True,
        handoff_reason=summary,
    )
    metadata = {"summary": summary, "handoff_group": group, "handoff_origen": origen}
    if error_type:
        metadata["error_type"] = error_type
    return AgentResult(
        message=final_message,
        intent="human_handoff",
        agent="handoff",
        metadata=response_plan_metadata(response_plan, metadata),
    )


class HandoffAgent:
    """Agente que prepara y ejecuta la derivacion a Odoo Chat."""

    def __init__(self) -> None:
        """Inicializa el handoffagent con la configuracion necesaria."""
        self.odoo = OdooChatClient()
        self.settings = get_settings()

    @staticmethod
    def _sales_state(state: SessionState) -> dict:
        """Devuelve el estado comercial."""
        return (state.metadata or {}).get("sales") or {}

    @staticmethod
    def _contact_state(state: SessionState) -> dict:
        """Devuelve el estado de contacto."""
        return (state.metadata or {}).get("contact") or {}

    def _handoff_group(self, state: SessionState) -> str:
        """Devuelve el grupo handoff."""
        configured = str((state.metadata or {}).get("handoff_group") or "").strip().lower()
        if configured:
            return configured
        sales_state = self._sales_state(state)
        pending_agent = str((state.metadata or {}).get("pending_agent") or "").strip().lower()
        current_intent = str(state.current_intent or "").strip().lower()
        if pending_agent == "sales":
            return "iainfo"
        if current_intent in {"sales", "commercial", "agencies"}:
            return "iainfo"
        if str(sales_state.get("pending_intent") or "").strip().lower() in {"commercial", "agencies"}:
            return "iainfo"
        if str(sales_state.get("last_intent") or "").strip().lower() in {"commercial", "agencies"}:
            return "iainfo"
        return "support"

    def _handoff_origen(self, state: SessionState) -> str:
        """Devuelve el origen handoff."""
        configured = str((state.metadata or {}).get("handoff_origen") or "").strip()
        if configured:
            return configured
        if self._handoff_group(state) == "iainfo":
            return self.settings.info_origen
        return "ia"

    def _handoff_summary(self, state: SessionState) -> str:
        """Devuelve el resumen handoff."""
        contact_state = self._contact_state(state)
        sales_state = self._sales_state(state)
        selected_contract = str(contact_state.get("selected_contract") or "").strip()
        pending_agent = str((state.metadata or {}).get("pending_agent") or "").strip().lower()
        current_intent = str(state.current_intent or "").strip().lower()
        recommended_plan = str(((sales_state.get("recommended_plan") or {}).get("name")) or "").strip()

        if self._handoff_group(state) == "iainfo":
            if recommended_plan:
                return f"Cliente solicita asesor especializado comercial. Plan sugerido: {recommended_plan}."
            return "Cliente solicita asesor especializado comercial."
        if selected_contract:
            return f"Cliente solicita asesor especializado. Contrato seleccionado: {selected_contract}."
        if pending_agent == "billing" or current_intent == "billing":
            return "Cliente solicita asesor especializado de facturación."
        if pending_agent == "support" or current_intent == "support":
            return "Cliente solicita asesor especializado de soporte."
        return f"Handoff solicitado para session_id={state.session_id}"

    def _handoff_customer_message(self, state: SessionState) -> str:
        """Devuelve el mensaje handoff customer."""
        contact_state = self._contact_state(state)
        sales_state = self._sales_state(state)
        group = self._handoff_group(state)
        selected_contract = str(contact_state.get("selected_contract") or "").strip()
        recommended_plan = str(((sales_state.get("recommended_plan") or {}).get("name")) or "").strip()
        if group == "iainfo" and recommended_plan:
            return (
                f"Voy a derivar su caso con un asesor especializado comercial para revisar el plan *{recommended_plan}* "
                "y ayudarle a avanzar. Ya le dejo el contexto."
            )
        if group == "iainfo":
            return "Voy a derivar su caso con un asesor especializado comercial. Ya le dejo el contexto."
        if selected_contract:
            return (
                f"Voy a derivar su caso con un asesor especializado para continuar con el contrato *{selected_contract}*. "
                "Ya le dejo el contexto."
            )
        return "Voy a derivar su caso con un asesor especializado. Ya le dejo el contexto."

    async def handle(self, message: InboundMessage, state: SessionState) -> AgentResult:
        """Maneja la entrada completa y devuelve el resultado final."""
        summary = self._handoff_summary(state)
        group = self._handoff_group(state)
        origen = self._handoff_origen(state)
        final_message = self._handoff_customer_message(state)
        if state.human_handoff:
            await self.odoo.relay_customer_message(
                channel=message.channel,
                recipient=message.recipient,
                message=(message.mensaje or "").strip() or "Cliente responde dentro del handoff humano.",
                cedula=message.cedula or state.cedula,
                origen=origen,
                group=group,
            )
            if message.attachments:
                await self.odoo.relay_attachments(
                    channel=message.channel,
                    recipient=message.recipient,
                    attachments=[item.model_dump() if hasattr(item, "model_dump") else item for item in (message.attachments or [])],
                    cedula=message.cedula or state.cedula,
                    origen=origen,
                    group=group,
                )
            return AgentResult(
                message="",
                intent="human_handoff",
                agent="handoff",
                metadata={
                    "summary": summary,
                    "skip_delivery": True,
                    "handoff_group": group,
                    "handoff_origen": origen,
                },
            )

        try:
            await self.odoo.create_handoff(
                summary,
                channel=message.channel,
                recipient=message.recipient,
                cedula=message.cedula or state.cedula,
                origen=origen,
                group=group,
            )
            if message.attachments:
                await self.odoo.relay_attachments(
                    channel=message.channel,
                    recipient=message.recipient,
                    attachments=[item.model_dump() if hasattr(item, "model_dump") else item for item in (message.attachments or [])],
                    cedula=message.cedula or state.cedula,
                    origen=origen,
                    group=group,
                )
        except httpx.ReadTimeout:
            success_message = final_message
            response_plan = build_handoff_response_plan(
                message=success_message,
                conversation_state="handoff_created",
                reply_goal="mantener la derivacion aunque odoo tarde en responder",
                summary=summary,
                target_group=group,
                hypothesis="customer_requested_specialist",
                next_step="wait_human_followup",
                should_handoff=True,
            )
            return AgentResult(
                message=success_message,
                intent="human_handoff",
                agent="handoff",
                metadata=response_plan_metadata(
                    response_plan,
                    {
                        "summary": summary,
                        "handoff_group": group,
                        "handoff_origen": origen,
                        "handoff_timeout": True,
                    },
                ),
            )
        except Exception:
            failed_message = (
                "Quise dejar su caso con un asesor especializado, pero justo ahora no pude completar esa derivación. "
                "Si lo prefiere, vuelva a escribirme en un momento y lo intento otra vez."
            )
            response_plan = build_handoff_response_plan(
                message=failed_message,
                conversation_state="handoff_failed",
                reply_goal="ser honesto cuando la derivación no pudo completarse",
                summary=summary,
                target_group=group,
                hypothesis="advisor_handoff_requested_but_failed",
                next_step="retry_handoff_later",
                should_handoff=False,
            )
            return AgentResult(
                message=failed_message,
                intent="clarify",
                agent="clarify",
                metadata=response_plan_metadata(
                    response_plan,
                    {"summary": summary, "handoff_group": group, "handoff_origen": origen, "handoff_failed": True},
                ),
            )
        response_plan = build_handoff_response_plan(
            message=final_message,
            conversation_state="handoff_created",
            reply_goal="derivar con calidez y dejar claro que el contexto ya quedó enviado",
            summary=summary,
            target_group=group,
            hypothesis="customer_requested_specialist",
            next_step="wait_human_followup",
            should_handoff=True,
        )
        return AgentResult(
            message=final_message,
            intent="human_handoff",
            agent="handoff",
            metadata=response_plan_metadata(
                response_plan,
                {"summary": summary, "handoff_group": group, "handoff_origen": origen},
            ),
        )
