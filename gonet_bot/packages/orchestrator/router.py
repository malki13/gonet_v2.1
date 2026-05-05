"""Reglas de ruteo de alto nivel para asignar el agente correcto."""

import re

from packages.agents.contact_utils import user_requests_human
from packages.orchestrator.conversation_classifier import ConversationClassifier
from packages.orchestrator.confidence import needs_clarification
from packages.shared.config import get_settings
from packages.shared.constants import PAYMENT_KEYWORDS
from packages.shared.schemas import InboundMessage, RouteDecision, SessionState
from packages.shared.utils import contains_any_phrase, normalize_text


class IntentRouter:
    """Decide a qué agente se envía cada mensaje a partir de la clasificación."""
    def __init__(self, *, classifier: ConversationClassifier | None = None) -> None:
        """Inicializa el intentrouter con la configuracion necesaria."""
        self.classifier = classifier or ConversationClassifier()

    async def decide(self, message: InboundMessage, state: SessionState) -> RouteDecision:
        """Decide la ruta del mensaje según la clasificación y el estado."""
        return await self._decide_internal(message=message, state=state, use_openai=True)

    async def heuristic_decide(self, message: InboundMessage, state: SessionState) -> RouteDecision:
        """Devuelve el decide heuristic."""
        return await self._decide_internal(message=message, state=state, use_openai=False)

    async def _decide_internal(self, *, message: InboundMessage, state: SessionState, use_openai: bool) -> RouteDecision:
        """Decide internal."""
        settings = get_settings()
        raw_text = str(message.mensaje or "").strip()
        normalized = normalize_text(message.mensaje)

        if self._is_message_too_long(raw_text, settings):
            return RouteDecision(
                agent="clarify",
                intent="clarify",
                confidence=0.2,
                reason="message_too_long",
                requires_clarification=True,
            )

        if self._looks_like_noise(raw_text):
            return RouteDecision(
                agent="clarify",
                intent="clarify",
                confidence=0.2,
                reason="message_noise",
                requires_clarification=True,
            )

        if user_requests_human(message.mensaje):
            return RouteDecision(
                agent="handoff",
                intent="human_handoff",
                confidence=0.99,
                reason="user_requested_human",
            )

        if message.attachments and contains_any_phrase(normalized, PAYMENT_KEYWORDS):
            return RouteDecision(
                agent="billing",
                intent="billing_proof",
                confidence=0.93,
                reason="attachment_plus_payment_context",
            )

        if use_openai:
            classification = await self.classifier.classify(message=message, state=state)
        else:
            classification = self.classifier.classify_heuristic(message=message, state=state)
        return self._decision_from_classification(classification)

    @staticmethod
    def _decision_from_classification(classification: dict) -> RouteDecision:
        """Traduce la clasificación en una decisión de ruteo."""
        mode = str(classification.get("conversation_mode") or "insufficient_signal").strip().lower()
        domain = str(classification.get("business_domain") or "none").strip().lower()
        reason = str(classification.get("reason") or mode or "insufficient_signal").strip().lower()
        try:
            confidence = float(classification.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.40

        if mode == "task":
            if domain == "billing":
                return RouteDecision(
                    agent="billing",
                    intent="billing",
                    confidence=confidence,
                    reason=reason or "semantic_billing",
                )
            if domain == "support":
                return RouteDecision(
                    agent="support",
                    intent="support",
                    confidence=confidence,
                    reason=reason or "semantic_support",
                )
            if domain == "sales":
                return RouteDecision(
                    agent="sales",
                    intent="sales",
                    confidence=confidence,
                    reason=reason or "semantic_sales",
                )

        requires_clarification = mode in {"greeting_only", "small_talk", "out_of_scope"} or needs_clarification(confidence)
        return RouteDecision(
            agent="clarify",
            intent="clarify",
            confidence=confidence,
            reason=reason or "insufficient_signal",
            requires_clarification=requires_clarification,
        )

    @staticmethod
    def _is_message_too_long(raw_text: str, settings) -> bool:
        """Indica si long mensaje too se cumple."""
        if len(raw_text) > settings.max_inbound_chars:
            return True
        return len([part for part in raw_text.split() if part]) > settings.max_inbound_words

    @staticmethod
    def _looks_like_noise(raw_text: str) -> bool:
        """Devuelve el noise looks like."""
        compact = re.sub(r"\s+", "", raw_text.lower())
        if len(compact) < 20:
            return False
        return bool(re.search(r"(.)\1{24,}", compact))
