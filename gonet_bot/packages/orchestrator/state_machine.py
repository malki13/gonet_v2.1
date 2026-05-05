"""Actualizacion de estado conversacional tras cada respuesta."""

from datetime import datetime, timezone

from packages.shared.schemas import AgentResult, RouteDecision, SessionState


def update_state(state: SessionState, decision: RouteDecision, result: AgentResult) -> SessionState:
    """Devuelve el estado update."""
    now = datetime.now(timezone.utc)
    awaiting_identity_turn = state.awaiting_field == "cedula"
    state.current_intent = result.intent or decision.intent
    state.last_agent = result.agent or decision.agent
    state.history.append({"role": "assistant", "content": result.message})
    state.history = state.history[-20:]
    state.updated_at = now
    state.last_assistant_message_at = now
    state.selected_contract = ((state.metadata.get("contact") or {}).get("selected_contract"))
    if result.intent == "ask_cedula":
        state.awaiting_field = "cedula"
        pending_agent = result.metadata.get("pending_agent")
        pending_message = result.metadata.get("pending_message")
        if pending_agent:
            state.metadata["pending_agent"] = pending_agent
        if pending_message:
            state.metadata["pending_message"] = pending_message
    else:
        pending_agent = str(state.metadata.get("pending_agent") or "").strip().lower()
        keep_pending_message = (
            awaiting_identity_turn
            and pending_agent in {"support", "billing"}
            and result.agent in {"support", "billing"}
        )
        state.awaiting_field = None
        state.metadata.pop("pending_agent", None)
        if not keep_pending_message:
            state.metadata.pop("pending_message", None)
    if result.agent == "handoff" or decision.agent == "handoff":
        state.human_handoff = True
        state.metadata["handoff_summary"] = result.metadata.get("summary")
        handoff_group = str(result.metadata.get("handoff_group") or "").strip()
        handoff_origen = str(result.metadata.get("handoff_origen") or "").strip()
        if handoff_group:
            state.metadata["handoff_group"] = handoff_group
        if handoff_origen:
            state.metadata["handoff_origen"] = handoff_origen
    return state
