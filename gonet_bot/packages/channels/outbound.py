"""Modelo y helpers para mensajes salientes."""

from packages.shared.schemas import AgentResult, OutboundMessage, RouteDecision


def build_outbound_message(decision: RouteDecision, result: AgentResult) -> OutboundMessage:
    """Construye mensaje outbound a partir del contexto disponible."""
    return OutboundMessage(
        message=result.message,
        agent=result.agent,
        intent=result.intent,
        confidence=decision.confidence,
        requires_clarification=decision.requires_clarification,
        actions=result.actions,
        metadata=result.metadata,
    )
