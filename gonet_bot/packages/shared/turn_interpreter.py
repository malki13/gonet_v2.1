"""Interpretación de turnos activos para decidir la siguiente acción."""

import logging

from packages.shared.schemas import FlowTurnInterpretation
from packages.shared.utils import normalize_text

logger = logging.getLogger("shared.turn_interpreter")

ACTIVE_FLOW_TURN_INTERPRETER_INSTRUCTIONS = (
    "Interpretas un turno dentro de un flujo activo de chatbot. "
    "Devuelve solo JSON valido con estas llaves: action, target_intent, confidence, reason, slot_updates. "
    "action puede ser: "
    "answer_current_step, switch_intent, show_catalog, start_capture, start_recommendation, "
    "accept_recommended_plan, accept_information, decline_registration, unclear. "
    "target_intent solo aplica si action = switch_intent. "
    "slot_updates debe ser un objeto JSON con los valores estructurados que se puedan inferir "
    "para el paso actual. Si no hay nada confiable, devuelve {}. "
    "Usa current_stage, current_question, allowed_actions, allowed_switch_intents, known_state, "
    "initial_analysis y recent_turns como contexto. "
    "Prefiere answer_current_step cuando el cliente parece estar respondiendo la pregunta actual, "
    "aunque mencione palabras que en otro contexto podrian parecer otro intent. "
    "Usa switch_intent solo si el cambio de tema es claro. "
    "Se conservador: si no hay suficiente señal, devuelve unclear. "
    "confidence debe ser un numero entre 0 y 1."
)


def _normalize_keyword(value: str | None) -> str:
    """Normaliza keyword."""
    return str(value or "").strip().lower()


class ActiveFlowTurnInterpreter:
    """el intérprete de activo flujo turno"""
    def __init__(self, *, llm, settings) -> None:
        """Inicializa el activeflowturninterpreter con la configuracion necesaria."""
        self.llm = llm
        self.settings = settings

    def _mode(self) -> str:
        """Devuelve el modo configurado para este flujo."""
        return str(getattr(self.settings, "flow_turn_interpreter_mode", "auto") or "auto").strip().lower()

    def _enabled(self) -> bool:
        """Indica si la integracion esta habilitada por configuracion."""
        if not bool(getattr(self.settings, "flow_turn_interpreter_enabled", True)):
            return False
        mode = self._mode()
        if mode == "heuristic":
            return False
        enabled_fn = getattr(self.llm, "enabled", None)
        if callable(enabled_fn):
            try:
                return bool(enabled_fn())
            except Exception:
                return False
        return False

    async def interpret(
        self,
        *,
        flow_name: str,
        current_stage: str,
        user_message: str,
        current_question: str | None,
        allowed_actions: tuple[str, ...],
        allowed_switch_intents: tuple[str, ...] = (),
        known_state: dict | None = None,
        recent_turns: list[dict[str, str]] | None = None,
        initial_analysis: dict | None = None,
        fallback: FlowTurnInterpretation | None = None,
    ) -> FlowTurnInterpretation:
        """Devuelve el interpret."""
        fallback = fallback or FlowTurnInterpretation(action="unclear", reason="fallback")
        if not self._enabled():
            return fallback

        extract_fn = getattr(self.llm, "extract_json", None)
        if not callable(extract_fn):
            return fallback

        payload = {
            "message": {
                "text": str(user_message or "").strip(),
                "normalized": normalize_text(user_message),
            },
            "flow_context": {
                "flow_name": flow_name,
                "current_stage": current_stage,
                "current_question": current_question,
                "allowed_actions": list(allowed_actions),
                "allowed_switch_intents": list(allowed_switch_intents),
                "known_state": known_state or {},
                "recent_turns": recent_turns or [],
                "initial_analysis": initial_analysis or {},
            },
        }
        try:
            result = await extract_fn(
                instructions=ACTIVE_FLOW_TURN_INTERPRETER_INSTRUCTIONS,
                payload=payload,
                max_output_tokens=140,
                temperature=0.0,
            )
        except Exception:
            logger.exception("active_flow_turn_interpreter_failed flow=%s stage=%s", flow_name, current_stage)
            return fallback

        if result.get("status") != "ok" or not isinstance(result.get("result"), dict):
            return fallback

        data = result.get("result") or {}
        action = _normalize_keyword(data.get("action"))
        if action not in allowed_actions:
            return fallback

        target_intent = _normalize_keyword(data.get("target_intent")) or None
        if action == "switch_intent" and target_intent not in allowed_switch_intents:
            return fallback

        confidence = data.get("confidence")
        if isinstance(confidence, int):
            confidence = float(confidence)
        if not isinstance(confidence, float):
            confidence = None
        elif confidence < 0.0 or confidence > 1.0:
            confidence = None

        interpretation = FlowTurnInterpretation(
            action=action,
            target_intent=target_intent,
            confidence=confidence,
            reason=str(data.get("reason") or "").strip() or "llm",
            slot_updates=data.get("slot_updates") if isinstance(data.get("slot_updates"), dict) else {},
        )
        logger.info(
            "active_flow_turn_interpreted flow=%s stage=%s action=%s target_intent=%s confidence=%s",
            flow_name,
            current_stage,
            interpretation.action,
            interpretation.target_intent,
            interpretation.confidence,
        )
        return interpretation
