"""Composición final de respuestas antes de salir al canal."""

import re
import unicodedata
from typing import Any

from packages.agents.contact_billing_utils import format_billing_duplicate_message
from packages.agents.contact_contract_utils import contract_code, contract_display_name, contract_due_value, format_money
from packages.integrations.openai_client import OpenAIClient
from packages.shared.assistant_persona import assistant_intro_prefix, message_has_assistant_intro
from packages.shared.config import get_settings
from packages.shared.schemas import AgentResult, InboundMessage, ResponsePlan, RouteDecision, SessionState

URL_RE = re.compile(r"https?://\S+")
MONEY_RE = re.compile(r"\$\s*\d[\d.,]*")
LONG_NUMBER_RE = re.compile(r"\b[A-Z]{0,4}-?\d{4,}\b", re.IGNORECASE)
NUMBERED_LINE_RE = re.compile(r"^\d+\.\s+.+$", re.MULTILINE)
WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
CONTENT_STOPWORDS = {
    "a",
    "al",
    "algo",
    "aqui",
    "con",
    "cuanto",
    "de",
    "del",
    "el",
    "ella",
    "en",
    "es",
    "esta",
    "este",
    "esto",
    "hola",
    "la",
    "las",
    "lo",
    "los",
    "mas",
    "me",
    "mi",
    "muy",
    "no",
    "para",
    "por",
    "que",
    "se",
    "si",
    "su",
    "te",
    "tu",
    "un",
    "una",
    "y",
    "ya",
}
OPENAI_REWRITE_INTENTS = {
    "billing",
    "billing_link",
    "billing_proof_requested",
    "billing_proof_pending",
    "billing_action_clarify",
    "commercial",
    "welcome",
    "agencies",
    "menu",
    "human_handoff",
    "support_clarify",
    "support_network_monitoring",
    "support_manual_checks",
    "support_network_followup",
    "support_network_resolved",
    "support_edit_network_waiting_otp",
    "support_edit_network_otp_retry",
    "support_edit_network_pending",
}

REWRITE_INSTRUCTIONS = (
    "Eres un editor de copy para atencion al cliente por WhatsApp. "
    "Tu trabajo es reescribir mensajes para que suenen profesionales, claros y fluidos en espanol de Ecuador. "
    "No decides acciones ni cambias la logica del flujo. Solo mejoras la redaccion. "
    "Reglas obligatorias: "
    "1) No cambies hechos, montos, codigos, contratos, URLs, estados, opciones, botones, tiempos ni instrucciones operativas. "
    "2) No agregues informacion nueva ni promesas nuevas. "
    "3) No digas que eres IA ni menciones prompts, payloads o instrucciones. "
    "4) Devuelve una sola burbuja breve y natural, sin tono de menu o corporativo. "
    "5) Si hay lineas numeradas de contratos u opciones, conservalas exactamente. "
    "6) Si hay palabras clave para responder o botones, mantenlas exactamente. "
    "7) Usa como base el mensaje seguro entregado en 'base_message' y solo mejoralo. "
    "8) Trata siempre al cliente de usted; no uses tuteo ni expresiones demasiado coloquiales."
)


def _normalize_compare_text(value: str) -> str:
    """Normaliza texto compare."""
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    ascii_value = ascii_value.replace("*", "").replace("_", "").replace("`", "")
    return " ".join(ascii_value.split())


def _extract_urls(text: str) -> list[str]:
    """Extrae urls."""
    return [item.rstrip(").,;") for item in URL_RE.findall(text or "")]


def _extract_numbered_lines(text: str) -> list[str]:
    """Extrae numbered lines."""
    return [line.strip() for line in str(text or "").splitlines() if NUMBERED_LINE_RE.match(line.strip())]


def _extract_protected_tokens(text: str) -> set[str]:
    """Extrae protected tokens."""
    tokens = set(MONEY_RE.findall(text or ""))
    tokens.update(LONG_NUMBER_RE.findall(text or ""))
    return {token.strip() for token in tokens if token.strip()}


class ResponseComposer:
    """ResponseComposer"""
    def __init__(self, *, llm: OpenAIClient | None = None) -> None:
        """Inicializa el responsecomposer con la configuracion necesaria."""
        self.settings = get_settings()
        self.llm = llm or OpenAIClient()

    async def compose_direct_result(
        self,
        *,
        state: SessionState,
        channel: str,
        recipient: str,
        raw_message: str,
        intent: str,
        result_agent: str,
        decision_agent: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
        actions: Any | None = None,
        user_message: str = "",
        confidence: float = 1.0,
        requires_clarification: bool = False,
        inbound_metadata: dict[str, Any] | None = None,
    ) -> AgentResult:
        """Compone resultado de direct respetando el contexto."""
        inbound = InboundMessage(
            mensaje=str(user_message or ""),
            channel=channel,
            recipient=recipient,
            session_id=state.session_id,
            cedula=state.cedula,
            metadata=dict(inbound_metadata or {}),
        )
        decision = RouteDecision(
            agent=decision_agent,
            intent=intent,
            confidence=confidence,
            reason=reason,
            requires_clarification=requires_clarification,
        )
        result = AgentResult(
            message=str(raw_message or ""),
            intent=intent,
            agent=result_agent,
            actions=actions,
            metadata=dict(metadata or {}),
        )
        return await self.compose(message=inbound, state=state, decision=decision, result=result)

    async def compose(
        self,
        *,
        message: InboundMessage,
        state: SessionState,
        decision: RouteDecision,
        result: AgentResult,
    ) -> AgentResult:
        """Devuelve el compose."""
        response_plan = self._response_plan(result)
        original = str(result.message or "").strip()
        planned_message = str((response_plan.message if response_plan else "") or "").strip()
        source_message = planned_message or original
        if not source_message or bool((result.metadata or {}).get("skip_delivery")):
            return result

        if result.intent == "billing_async_result":
            candidate = self._compose_billing(result=result, state=state, original=source_message) or source_message
            mode_used = "agent" if candidate == source_message else "heuristic"
            if self._enabled():
                candidate = self._prepend_intro_if_needed(candidate=candidate, state=state, result=result)
            if not candidate or candidate == original:
                return result
            metadata = dict(result.metadata or {})
            metadata["response_composer"] = {
                "applied": True,
                "mode": mode_used,
            }
            return result.model_copy(update={"message": candidate, "metadata": metadata})

        if not self._enabled():
            if not planned_message:
                return result
            candidate = source_message
            mode_used = "response_plan" if planned_message and planned_message != original else "disabled"
        else:
            if self._agent_owns_copy(result=result, response_plan=response_plan):
                candidate = source_message
                mode_used = "response_plan" if planned_message else "agent"
            else:
                heuristic = self._rewrite_heuristic(
                    original=source_message,
                    message=message,
                    state=state,
                    decision=decision,
                    result=result,
                    response_plan=response_plan,
                )
                candidate = source_message if heuristic == source_message else self._finalize(heuristic, original=source_message)
                mode_used = "response_plan" if planned_message else "heuristic"

                if self._should_use_openai(
                    result=result,
                    original=source_message,
                    base_message=candidate,
                    response_plan=response_plan,
                ):
                    rendered = await self._rewrite_with_openai(
                        original=source_message,
                        base_message=candidate,
                        message=message,
                        state=state,
                        decision=decision,
                        result=result,
                        response_plan=response_plan,
                    )
                    if rendered:
                        validated = self._finalize(rendered, original=source_message)
                        if validated != source_message:
                            candidate = validated
                            mode_used = "openai"
        if self._enabled():
            candidate = self._prepend_intro_if_needed(candidate=candidate, state=state, result=result)
        if not candidate or candidate == original:
            return result

        metadata = dict(result.metadata or {})
        metadata["response_composer"] = {
            "applied": True,
            "mode": mode_used,
        }
        return result.model_copy(update={"message": candidate, "metadata": metadata})

    @staticmethod
    def _response_plan(result: AgentResult) -> ResponsePlan | None:
        """Devuelve el plan de respuesta."""
        raw = (result.metadata or {}).get("response_plan")
        if isinstance(raw, ResponsePlan):
            return raw
        if isinstance(raw, dict):
            try:
                return ResponsePlan.model_validate(raw)
            except Exception:
                return None
        return None

    def _enabled(self) -> bool:
        """Indica si la integracion esta habilitada por configuracion."""
        return bool(self.settings.conversational_renderer_enabled)

    def _mode(self) -> str:
        """Devuelve el modo configurado para este flujo."""
        return str(self.settings.conversational_renderer_mode or "auto").strip().lower()

    @staticmethod
    def _agent_owns_copy(*, result: AgentResult, response_plan: ResponsePlan | None = None) -> bool:
        """Devuelve el copy agent owns."""
        if str(result.intent or "").strip().lower() in {"ask_cedula", "consent_required", "contract_selection"}:
            return True
        if str(result.intent or "").strip().lower() == "session_closed":
            return False
        domain = str((response_plan.domain if response_plan else "") or "").strip().lower()
        agent = str(result.agent or "").strip().lower()
        return domain in {"sales", "support", "billing", "handoff"} or agent in {"sales", "support", "billing", "handoff"}

    def _should_use_openai(
        self,
        *,
        result: AgentResult,
        original: str,
        base_message: str,
        response_plan: ResponsePlan | None = None,
    ) -> bool:
        """Indica si openai use se cumple."""
        if self._agent_owns_copy(result=result, response_plan=response_plan):
            return False
        if response_plan is not None and str(response_plan.message or "").strip():
            return False
        mode = self._mode()
        if mode == "heuristic":
            return False
        if result.intent not in OPENAI_REWRITE_INTENTS:
            return False
        if not base_message:
            return False
        if not (_extract_urls(original) or _extract_protected_tokens(original) or _extract_numbered_lines(original)):
            original_words = [token for token in WORD_RE.findall(_normalize_compare_text(original)) if token not in CONTENT_STOPWORDS]
            if len(original_words) <= 2:
                return False
        if mode == "openai":
            return True
        if mode == "auto":
            enabled_fn = getattr(self.llm, "enabled", None)
            if callable(enabled_fn):
                try:
                    return bool(enabled_fn())
                except Exception:
                    return False
            return True
        return False

    def _finalize(self, candidate: str, *, original: str) -> str:
        """Limpia el estado temporal y captura errores no observados."""
        cleaned = self._normalize_message(candidate)
        if not cleaned:
            return original
        if len(cleaned) > max(self.settings.conversational_renderer_max_chars, len(original) + 40):
            return original
        if self._looks_unsafe(cleaned):
            return original
        if not self._preserves_protected_content(original, cleaned):
            return original
        if not self._has_meaningful_overlap(original, cleaned):
            return original
        return cleaned

    @staticmethod
    def _normalize_message(text: str) -> str:
        """Normaliza mensaje."""
        normalized_text = str(text or "").replace("\r\n", "\n").strip()
        if normalized_text.startswith("```") and normalized_text.endswith("```"):
            normalized_text = normalized_text.strip("`").strip()
        lines = [re.sub(r"\s+", " ", line).strip() for line in normalized_text.split("\n")]
        kept = [line for line in lines if line]
        normalized = "\n\n".join(kept)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        normalized = re.sub(r"^(?:mensaje final|respuesta final|mensaje)\s*:\s*", "", normalized, flags=re.IGNORECASE)
        if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
            normalized = normalized[1:-1].strip()
        return normalized.strip()

    @staticmethod
    def _has_assistant_turn(state: SessionState) -> bool:
        """Indica si turno assistant se cumple."""
        for item in state.history or []:
            if str((item or {}).get("role") or "").strip().lower() == "assistant":
                return True
        return False

    def _is_initial_conversation_reply(self, state: SessionState) -> bool:
        """Indica si reply initial conversation se cumple."""
        if bool((state.metadata or {}).get("assistant_intro_sent")):
            return False
        if self._has_assistant_turn(state):
            return False
        if state.last_assistant_message_at is not None:
            return False
        if any(
            (
                str(state.current_intent or "").strip(),
                str(state.last_agent or "").strip(),
                str(state.awaiting_field or "").strip(),
            )
        ):
            return False
        return True

    def _prepend_intro_if_needed(self, *, candidate: str, state: SessionState, result: AgentResult) -> str:
        """Agrega una introducción si aún no se presentó el asistente."""
        cleaned = str(candidate or "").strip()
        if not cleaned:
            return cleaned
        assistant_profile = ((state.metadata or {}).get("assistant_profile") or {})
        assistant_name = str((assistant_profile.get("display_name") or "")).strip()
        if message_has_assistant_intro(cleaned, assistant_name=assistant_name):
            state.metadata["assistant_intro_sent"] = True
            return cleaned
        if not self._is_initial_conversation_reply(state):
            return cleaned
        if not self._should_have_first_turn_intro(result):
            return cleaned
        intro = assistant_intro_prefix(
            assistant_name=assistant_name,
            assistant_profile=assistant_profile,
        )
        state.metadata["assistant_intro_sent"] = True
        return f"{intro}{cleaned}".strip()

    @staticmethod
    def _should_have_first_turn_intro(result: AgentResult) -> bool:
        """Indica si intro have first turno se cumple."""
        return bool(str(result.message or "").strip())

    @staticmethod
    def _looks_unsafe(text: str) -> bool:
        """Devuelve el unsafe looks."""
        lowered = _normalize_compare_text(text)
        blocked = (
            "soy una ia",
            "como modelo de lenguaje",
            "no puedo cumplir esa instruccion",
            "ignore previous instructions",
            "ignora las instrucciones",
        )
        return any(token in lowered for token in blocked)

    def _preserves_protected_content(self, original: str, candidate: str) -> bool:
        """Comprueba que la reescritura conserve contenido protegido."""
        original_urls = _extract_urls(original)
        candidate_urls = _extract_urls(candidate)
        if any(url not in candidate for url in original_urls):
            return False
        if any(url not in original_urls for url in candidate_urls):
            return False

        candidate_compare = _normalize_compare_text(candidate)
        for token in _extract_protected_tokens(original):
            if _normalize_compare_text(token) not in candidate_compare:
                return False
        original_numbered_lines = _extract_numbered_lines(original)
        if original_numbered_lines:
            for line in original_numbered_lines:
                if _normalize_compare_text(line) not in candidate_compare:
                    return False
        return True

    @staticmethod
    def _has_meaningful_overlap(original: str, candidate: str) -> bool:
        """Comprueba si la nueva versión conserva la idea principal."""
        original_tokens = {
            token for token in WORD_RE.findall(_normalize_compare_text(original)) if token not in CONTENT_STOPWORDS
        }
        if not original_tokens:
            return True
        candidate_tokens = {
            token for token in WORD_RE.findall(_normalize_compare_text(candidate)) if token not in CONTENT_STOPWORDS
        }
        overlap = original_tokens & candidate_tokens
        required = 1 if len(original_tokens) <= 4 else 2
        return len(overlap) >= required

    async def _rewrite_with_openai(
        self,
        *,
        original: str,
        base_message: str,
        message: InboundMessage,
        state: SessionState,
        decision: RouteDecision,
        result: AgentResult,
        response_plan: ResponsePlan | None = None,
    ) -> str:
        """Reescribe el texto con OpenAI usando el estilo esperado."""
        payload = self._build_openai_payload(
            original=original,
            base_message=base_message,
            message=message,
            state=state,
            decision=decision,
            result=result,
            response_plan=response_plan,
        )
        rewritten = await self.llm.rewrite_text(
            instructions=REWRITE_INSTRUCTIONS,
            payload=payload,
            max_output_tokens=260,
            temperature=0.45,
        )
        if rewritten.get("status") != "ok":
            return ""
        return str(rewritten.get("text") or "").strip()

    def _build_openai_payload(
        self,
        *,
        original: str,
        base_message: str,
        message: InboundMessage,
        state: SessionState,
        decision: RouteDecision,
        result: AgentResult,
        response_plan: ResponsePlan | None = None,
    ) -> dict[str, Any]:
        """Construye payload openai a partir del contexto disponible."""
        metadata = result.metadata or {}
        contract = metadata.get("contract") or {}
        code = contract_code(contract)
        name = contract_display_name(contract)
        due_value = contract_due_value(contract)
        assistant_name = ((state.metadata or {}).get("assistant_profile") or {}).get("display_name")
        actions = self._action_labels(result.actions)
        facts: dict[str, Any] = {
            "agent": result.agent,
            "intent": result.intent,
            "decision_reason": decision.reason,
            "channel": message.channel,
            "assistant_name": assistant_name,
            "current_intent": state.current_intent,
            "awaiting_field": state.awaiting_field,
            "user_message": str(message.mensaje or "").strip(),
            "has_actions": bool(result.actions),
            "action_labels": actions,
            "issue_type": metadata.get("issue_type"),
            "handoff_summary": metadata.get("summary"),
            "coalesced_count": int((message.metadata or {}).get("coalesced_count") or 1),
        }
        if code:
            facts["contract_code"] = code
        if name:
            facts["customer_name"] = name
        if due_value > 0:
            facts["pending_value"] = f"${format_money(due_value)}"
        status = str(contract.get("status_label") or contract.get("state") or contract.get("status") or "").strip()
        if status:
            facts["service_status"] = status
        if response_plan is not None:
            facts["reply_goal"] = response_plan.reply_goal
            facts["hypothesis"] = response_plan.hypothesis
            facts["next_step"] = response_plan.next_step
        return {
            "style_profile": self._style_profile(result=result, response_plan=response_plan),
            "facts": facts,
            "base_message": base_message or original,
            "original_message": original,
            "response_plan": response_plan.model_dump(exclude_none=True) if response_plan is not None else None,
            "must_preserve": {
                "urls": _extract_urls(original),
                "protected_tokens": sorted(_extract_protected_tokens(original)),
                "numbered_lines": _extract_numbered_lines(original),
                "quoted_actions": actions,
            },
            "max_chars": self.settings.conversational_renderer_max_chars,
        }

    @staticmethod
    def _action_labels(actions: Any) -> list[str]:
        """Devuelve el labels action."""
        if not actions:
            return []
        if isinstance(actions, dict):
            buttons = actions.get("buttons") or []
            labels = [str((button or {}).get("title") or "").strip() for button in buttons]
            return [label for label in labels if label]
        if isinstance(actions, list):
            labels = [str((button or {}).get("title") or "").strip() for button in actions if isinstance(button, dict)]
            return [label for label in labels if label]
        return []

    @staticmethod
    def _style_profile(*, result: AgentResult, response_plan: ResponsePlan | None = None) -> dict[str, Any]:
        """Devuelve el perfil style."""
        base = {
            "tone": "humano, cercano y natural",
            "channel": "whatsapp",
            "avoid": [
                "tono de menu",
                "tono corporativo rigido",
                "frases redundantes",
                "presentaciones repetidas",
                "explicaciones largas",
            ],
            "prefer": [
                "frases cortas",
                "una sola idea por parrafo",
                "empatia breve",
                "cierres naturales",
            ],
        }
        if result.intent in {"billing", "billing_link", "billing_proof_requested", "billing_action_clarify"}:
            base["goal"] = "cobro claro pero humano"
        elif result.intent == "billing_async_result":
            base["goal"] = "seguimiento async de pagos con tono humano y claro"
        elif result.intent in {"support_clarify", "support_network_monitoring", "support_manual_checks", "support_network_followup"}:
            base["goal"] = "acompanamiento tecnico conversacional"
        elif result.intent in {"contract_selection", "consent_required", "ask_cedula"}:
            base["goal"] = "pedir dato sensible sin sonar formulario"
        elif result.intent == "human_handoff":
            base["goal"] = "escalar con calidez y sin sonar robot"
        elif result.intent == "session_closed":
            base["goal"] = "cerrar la conversacion con calidez y sin sonar administrativo"
        elif result.agent == "sales":
            base["goal"] = "asesoria comercial conversacional y natural"
        else:
            base["goal"] = "respuesta conversacional"
        if response_plan is not None and response_plan.reply_goal:
            base["goal"] = response_plan.reply_goal
        return base

    def _rewrite_heuristic(
        self,
        *,
        original: str,
        message: InboundMessage,
        state: SessionState,
        decision: RouteDecision,
        result: AgentResult,
        response_plan: ResponsePlan | None = None,
    ) -> str:
        """Reescribe el texto con reglas locales cuando no se usa OpenAI."""
        if response_plan is not None and str(response_plan.message or "").strip():
            return str(response_plan.message).strip()
        if result.intent == "ask_cedula":
            rewritten = self._compose_identity_request(result=result, original=original, state=state)
            if rewritten:
                return rewritten
        if result.intent == "session_closed":
            rewritten = self._compose_session_closed(result=result)
            if rewritten:
                return rewritten
        if result.intent == "consent_required":
            rewritten = self._compose_information_consent(result=result)
            if rewritten:
                return rewritten
        if result.intent == "contract_selection":
            rewritten = self._compose_contract_selection(result=result, original=original)
            if rewritten:
                return rewritten
        return self._generic_cleanup(original)

    def _compose_identity_request(self, *, result: AgentResult, original: str, state: SessionState) -> str:
        """Compone identity request respetando el contexto."""
        normalized = _normalize_compare_text(original)
        pending_agent = str((result.metadata or {}).get("pending_agent") or "").strip().lower()
        if "no encontre contratos con ese documento" in normalized:
            return (
                "No encontré contratos con ese documento. "
                "Si el servicio está a nombre de otra persona, compártame la cédula o RUC del titular y lo reviso."
            )
        if pending_agent == "handoff":
            return "Con gusto lo derivo con un asesor especializado. Para continuar, compártame la cédula o RUC del titular y dejo su caso listo."
        if "ese es el nombre del titular" in normalized:
            if "valores, pagos o cortes" in normalized:
                return "Ese es el nombre del titular. Lo que necesito es el número de cédula o RUC para revisar valores, pagos o cortes."
            return "Ese es el nombre del titular. Lo que necesito es el número de cédula o RUC para revisar el servicio."
        if "parece un comprobante de pago" in normalized:
            assistant_profile = ((state.metadata or {}).get("assistant_profile") or {})
            assistant_name = str((assistant_profile.get("display_name") or "")).strip()
            is_first_turn = not (state.history or [])
            original_has_intro = message_has_assistant_intro(original, assistant_name=assistant_name)
            if is_first_turn or original_has_intro:
                if state is not None:
                    state.metadata["assistant_intro_sent"] = True
                if assistant_name:
                    intro = assistant_intro_prefix(
                        assistant_name=assistant_name,
                        assistant_profile=assistant_profile,
                    )
                    return f"{intro}Ya vi la imagen y parece un comprobante de pago. Para revisarlo, compártame la cédula o RUC del titular del contrato."
                intro = assistant_intro_prefix(assistant_name=None, assistant_profile=assistant_profile)
                return f"{intro}Ya vi la imagen y parece un comprobante de pago. Para revisarlo, compártame la cédula o RUC del titular del contrato."
            return "Ya vi la imagen y parece un comprobante de pago. Para revisarlo, compártame la cédula o RUC del titular del contrato."
        if "sin ese dato no puedo revisarlo por aqui" in normalized:
            return "Sin ese dato no puedo revisarlo por aquí. Cuando la tenga, me escribe y seguimos."
        if "sin la cedula o ruc del titular del contrato no puedo" in normalized:
            return "Sin la cédula o RUC del titular no puedo abrir la revisión por aquí. Cuando la tenga, me la envía y seguimos."

        tip = ""
        tip_match = re.search(r"(Mientras la consigues[^.]*\.)", original, flags=re.IGNORECASE)
        if tip_match:
            tip = tip_match.group(1).strip()
        if "ya pagaste" in normalized and "comprobante" in normalized:
            tip = "Si ya realizó el pago, cuando lo tenga también reviso el comprobante."
        if tip:
            return f"Para revisarlo sí necesito la cédula o RUC del titular. {tip} Cuando la tenga, me la envía y seguimos."
        if pending_agent == "billing":
            return "Con gusto le ayudo con eso. Para revisarlo necesito la cédula o RUC del titular."
        return "Con gusto le ayudo con eso. Para revisarlo necesito la cédula o RUC del titular."

    def _compose_information_consent(self, *, result: AgentResult) -> str:
        """Compone information consent respetando el contexto."""
        contract = (result.metadata or {}).get("contract") or {}
        name = contract_display_name(contract)
        prefix = f"{name}, " if name else ""
        return f"{prefix}ya encontré su contrato. ¿Me confirma si acepta el uso de la información de GoNet para continuar con su atención?"

    @staticmethod
    def _compose_session_closed(*, result: AgentResult) -> str:
        """Compone session closed respetando el contexto."""
        if bool((result.metadata or {}).get("human_handoff")):
            return "Voy cerrando este chat por inactividad. Si luego desea retomar el caso, escríbanos por aquí y seguimos."
        return "Voy cerrando este chat por inactividad. Si luego necesita algo más, escríbame por aquí y retomamos."

    def _compose_contract_selection(self, *, result: AgentResult, original: str) -> str:
        """Compone contract selection respetando el contexto."""
        options = [line.strip() for line in original.splitlines() if NUMBERED_LINE_RE.match(line.strip())]
        if not options:
            return ""
        normalized = _normalize_compare_text(original)
        name_match = re.match(r"([^,\n]+),\s+vi varios contratos", original.strip(), flags=re.IGNORECASE)
        name = name_match.group(1).strip() if name_match else ""
        count = len(options)
        count_label = "varios contratos" if count <= 1 else f"{count} contratos"
        intro = f"{name}, veo {count_label} asociados a su cédula. ¿Cuál revisamos?" if name else f"Veo {count_label} asociados a su cédula. ¿Cuál revisamos?"
        if "puedes responder con el numero" in normalized:
            helper = "Respóndame con el número, por ejemplo *1* o *2*."
        else:
            helper = "Respóndame con el número que desea revisar."
        return "\n\n".join([intro, helper, "\n".join(options)])

    def _compose_billing(self, *, result: AgentResult, state: SessionState, original: str) -> str:
        """Compone billing respetando el contexto."""
        contract = (result.metadata or {}).get("contract") or {}
        code = contract_code(contract)
        name = contract_display_name(contract)
        due_value = contract_due_value(contract)
        url = _extract_urls(original)
        direct_url = url[0] if url else ""
        normalized = _normalize_compare_text(original)

        if result.intent == "billing_async_result":
            if "no pude validar bien el comprobante" in normalized and "foto mas clara" in normalized:
                return (
                    "Todavía no pude validarlo bien. ¿Podría enviarme una foto clara o el archivo completo otra vez, por favor, "
                    "procurando que se vean el número del documento, la fecha y el monto?"
                )
            if (
                ("ya esta registrado" in normalized and "no es valido" in normalized)
                or "ya fue registrado anteriormente" in normalized
            ):
                return format_billing_duplicate_message(escalate="asesor especializado" in normalized)
            if "no fue posible procesar el pago automaticamente" in normalized and "15 dias permitidos" in normalized:
                return (
                    "Sí pude revisar el comprobante, pero la fecha supera los 15 días permitidos. "
                    "Por eso necesito dejarlo con un asesor especializado para que lo revise."
                )
            if "no pude registrar el pago automaticamente" in normalized and "asesor" in normalized:
                return (
                    "Sí pude revisar el comprobante, pero no logré registrar el pago automáticamente. "
                    "Voy a dejarlo con un asesor especializado para que continúe con la revisión."
                )
            if "no pude dejarlo con un asesor" in normalized:
                return (
                    "Su comprobante necesita revisión manual, pero justo ahora no pude dejarlo con un asesor especializado. "
                    "Por favor inténtelo nuevamente en unos minutos."
                )

        if result.intent == "billing" and code:
            first = f"{name}, ya revisé su contrato *{code}*." if name else f"Ya revisé su contrato *{code}*."
            parts = [first]
            if due_value > 0:
                parts.append(f"Registra un pago pendiente de *${format_money(due_value)} más impuestos*.")
            parts.append("Si ya realizó el pago, envíeme el comprobante y lo reviso.")
            parts.append('Para continuar por aquí, escriba *Registrar Pago*, *Link de Cobro* o *asesor especializado*.')
            if direct_url:
                parts.append(f"Enlace directo: {direct_url}")
            return "\n\n".join(parts)

        if result.intent == "billing_link" and direct_url:
            return f"Claro, aquí tiene el enlace de pago: {direct_url}"

        if result.intent == "billing_proof_requested":
            return "Perfecto. Envíeme el comprobante en imagen o PDF y lo reviso."

        if result.intent == "billing_proof_pending":
            if "ocr legacy" in _normalize_compare_text(original):
                return "Ya recibí el comprobante. Sigo pendiente de la validación y le escribiré apenas tenga novedades."
            return "Estoy pendiente del comprobante. En cuanto quede validado, le avisaré por aquí."

        if result.intent == "billing_action_clarify":
            return "Para continuar por aquí, escriba *Registrar Pago*, *Link de Cobro* o *asesor especializado*."

        return ""

    @staticmethod
    def _generic_cleanup(text: str) -> str:
        """Devuelve el cleanup generic."""
        cleaned = ResponseComposer._normalize_message(text)
        replacements = [
            ("En este momento", "Ahora mismo"),
            ("En este momento registras", "Ahora mismo registra"),
            ("También puedes abrir este enlace directamente:", "Si lo prefiere, aquí tiene el enlace directo:"),
            ("También puedes abrir directamente este enlace:", "Si lo prefiere, aquí tiene el enlace directo:"),
            ("Voy a dejar tu caso con un asesor.", "Voy a dejar su caso con un asesor especializado."),
            ("Voy a dejar tu caso con un asesor especializado para que continúe la revisión contigo.", "Voy a dejar su caso con un asesor especializado para que continúe con la revisión. Ya le dejo el contexto."),
            ("Compárteme", "Compártame"),
            ("Dime cuál quieres revisar:", "Indíqueme cuál desea revisar."),
            ("Cuéntame si necesitas soporte, facturación o planes.", "Indíqueme si necesita soporte, facturación o planes."),
            ("Cuéntame, ¿es soporte técnico, facturación o planes?", "Indíqueme si su consulta es soporte, facturación o planes."),
        ]
        for old, new in replacements:
            cleaned = cleaned.replace(old, new)
        return cleaned.strip()
