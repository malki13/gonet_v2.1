"""Politicas duras que cortan el flujo normal del router."""

from packages.agents.contact_utils import user_requests_human
from packages.shared.schemas import InboundMessage, SessionState


def should_force_handoff(message: InboundMessage, state: SessionState) -> bool:
    """Indica si handoff force se cumple."""
    if state.human_handoff:
        return True
    return user_requests_human(message.mensaje)


def should_require_identity(message: InboundMessage, state: SessionState) -> bool:
    """Indica si identity require se cumple."""
    return not bool(state.cedula or message.cedula)
