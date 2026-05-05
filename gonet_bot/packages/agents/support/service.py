"""Agente de soporte de alto nivel."""

import logging

from packages.agents.contact_flow import ContactFlowService
from packages.agents.handoff.service import create_direct_handoff_result
from packages.integrations.odoo_chat import OdooChatClient
from packages.shared.schemas import AgentResult, InboundMessage, SessionState

logger = logging.getLogger("agents.support")


class SupportAgent:
    """Agente de soporte que reutiliza el flujo por contrato."""

    def __init__(self) -> None:
        """Inicializa el supportagent con la configuracion necesaria."""
        self.contact = ContactFlowService()
        self.handoff = OdooChatClient()

    async def handle(self, message: InboundMessage, state: SessionState) -> AgentResult:
        """Maneja la entrada completa y devuelve el resultado final."""
        try:
            return await self.contact.handle(preferred_domain="support", message=message, state=state)
        except Exception as exc:
            logger.exception("internal_contact_support_failed session_id=%s", message.session_id)
            summary = (
                "Escalamiento automático por falla operativa en soporte. "
                f"session_id={message.session_id}. "
                f"recipient={message.recipient}. "
                f"mensaje_usuario={(message.mensaje or '').strip()[:240]!r}. "
                f"error={exc.__class__.__name__}: {exc}"
            )
            return await create_direct_handoff_result(
                odoo=self.handoff,
                message=message,
                state=state,
                summary=summary,
                group="support",
                origen="ia",
                final_message=(
                    "Tuve un inconveniente interno mientras revisaba su caso y voy a dejarlo "
                    "con un asesor especializado para continuar con la atención."
                ),
                failure_message=(
                    "Tuve un inconveniente interno mientras revisaba su caso y tampoco pude completar "
                    "la derivación automática con un asesor especializado. Por favor, vuelva a escribir en unos minutos."
                ),
                hypothesis="internal_support_flow_error",
                error_type=exc.__class__.__name__,
            )
