"""orquestador principal de la conversacion."""

import logging
import re
import unicodedata
from datetime import datetime, timezone

from packages.agents.contact_utils import format_contract_holder_identity_request
from packages.agents.billing.service import BillingAgent
from packages.agents.handoff.service import HandoffAgent
from packages.agents.sales.service import SalesAgent
from packages.agents.support.service import SupportAgent
from packages.channels.media_proxy import build_public_media_url, store_temp_base64_media
from packages.channels.outbound import build_outbound_message
from packages.integrations.contact_registry import build_contact_registry
from packages.integrations.openai_client import OpenAIClient
from packages.integrations.speech_to_text import SpeechToTextService
from packages.orchestrator.policies import should_force_handoff, should_require_identity
from packages.orchestrator.orchestrator_copy import (
    format_billing_proof_identity_request,
    format_clarify_message,
    format_identity_name_clarification,
    format_openai_runtime_handoff_message,
    format_system_error_handoff_failure_message,
    format_system_error_handoff_message,
)
from packages.orchestrator.response_composer import ResponseComposer
from packages.orchestrator.router import IntentRouter
from packages.orchestrator.session_context import SessionContextService
from packages.orchestrator.state_machine import update_state
from packages.shared.response_planner import build_clarify_response_plan, build_handoff_response_plan, response_plan_metadata
from packages.shared.assistant_persona import (
    assistant_intro_prefix,
    ensure_assistant_greeting_style,
    pick_stable_option,
)
from packages.shared.config import get_settings
from packages.shared.identity import extract_identity_document
from packages.shared.sales_intents import analyze_sales_message
from packages.agents.contact_utils import user_requests_human
from packages.shared.schemas import AgentResult, FlowTurnInterpretation, InboundMessage, OutboundMessage, RouteDecision, SessionState
from packages.shared.turn_interpreter import ActiveFlowTurnInterpreter

logger = logging.getLogger("orchestrator")
SOFT_IDENTITY_HANDOFF_MESSAGE = "Dame un momento por favor. Voy a dejar su caso con un asesor especializado."


class OrchestratorService:
    """Coordina la recepción del mensaje, la elección del agente y la persistencia de la sesión."""

    def __init__(self) -> None:
        """Inicializa el servicio principal que orquesta la conversacion completa con la configuracion necesaria."""
        self.settings = get_settings()
        self.router = IntentRouter()
        classifier = getattr(self.router, "classifier", None)
        router_llm = getattr(classifier, "llm", None)
        self.turn_interpreter = ActiveFlowTurnInterpreter(llm=router_llm, settings=self.settings)
        self.sessions = SessionContextService()
        self.contact_registry = build_contact_registry()
        self.speech_to_text = SpeechToTextService()
        self.support = SupportAgent()
        self.billing = BillingAgent()
        self.sales = SalesAgent()
        self.handoff = HandoffAgent()
        self.response_composer = ResponseComposer()

    async def handle_message(self, message: InboundMessage) -> OutboundMessage:
        """Maneja mensaje y avanza el flujo."""
        state: SessionState | None = None
        try:
            message = await self._maybe_preprocess_audio_message(message)
            state = await self.sessions.load(message)
            if self._is_duplicate_inbound(message, state):
                logger.info(
                    "orchestrator_duplicate_inbound session_id=%s channel=%s recipient=%s message_id=%s",
                    message.session_id,
                    message.channel,
                    message.recipient,
                    (message.metadata or {}).get("message_id"),
                )
                return OutboundMessage(
                    message="",
                    agent="system",
                    intent="duplicate_inbound",
                    confidence=1.0,
                    metadata={"skip_delivery": True},
                )
            self._assistant_profile_for_state(state)
            message = self._hydrate_identity(message, state)
            if message.cedula and (not state.cedula or state.awaiting_field == "cedula"):
                state.cedula = message.cedula
                state.metadata.pop("missing_identity_attempts", None)
            await self.contact_registry.touch_contact(
                recipient=message.recipient,
                red=message.channel,
                identificacion=message.cedula or state.cedula,
                session_id=message.session_id,
                chat_preview=(message.mensaje or "").strip() or None,
            )
            logger.info(
                "orchestrator_start session_id=%s channel=%s recipient=%s current_intent=%s awaiting_field=%s human_handoff=%s attachments=%s preview=%r",
                message.session_id,
                message.channel,
                message.recipient,
                state.current_intent,
                state.awaiting_field,
                state.human_handoff,
                len(message.attachments or []),
                " ".join((message.mensaje or "").split())[:160],
            )
            state.history.append({"role": "user", "content": message.mensaje})
            now = datetime.now(timezone.utc)
            state.updated_at = now
            state.last_user_message_at = now
            sales_state = (state.metadata or {}).get("sales") or {}
            contact_state = (state.metadata or {}).get("contact") or {}
            support_state = (contact_state.get("support") or {})

            if should_force_handoff(message, state):
                if user_requests_human(message.mensaje) and not bool(state.cedula or message.cedula):
                    decision = RouteDecision(
                        agent="clarify",
                        intent="ask_cedula",
                        confidence=1.0,
                        reason="handoff_identity_required",
                        requires_clarification=True,
                    )
                    result = self._build_identity_request_result(
                        pending_agent="handoff",
                        pending_message=(message.mensaje or "").strip() or None,
                    )
                else:
                    decision = RouteDecision(
                        agent="handoff",
                        intent="human_handoff",
                        confidence=1.0,
                        reason="policy_force_handoff",
                    )
                    result = await self.handoff.handle(message, state)
            elif self._should_direct_handoff_for_openai_outage():
                decision, result = await self._handle_openai_runtime_unavailable(message=message, state=state)
            elif state.awaiting_field == "cedula" and message.cedula and (state.metadata.get("pending_agent") in {"support", "billing", "handoff"}):
                pending_agent = state.metadata.get("pending_agent")
                if pending_agent == "handoff":
                    decision = RouteDecision(
                        agent="handoff",
                        intent="human_handoff",
                        confidence=0.99,
                        reason="pending_identity_resolved_for_handoff",
                    )
                    result = await self._execute(decision, message, state)
                else:
                    decision = RouteDecision(
                        agent=pending_agent,
                        intent=pending_agent,
                        confidence=0.99,
                        reason="pending_identity_resolved",
                    )
                    result = await self._execute(decision, message, state)
            elif state.awaiting_field == "cedula" and (state.metadata.get("pending_agent") in {"support", "billing"}):
                pending_agent = state.metadata.get("pending_agent")
                decision = RouteDecision(
                    agent="clarify",
                    intent="ask_cedula",
                    confidence=0.99,
                    reason="pending_identity_missing",
                    requires_clarification=True,
                )
                if pending_agent == "billing" and message.attachments:
                    self._cache_pending_billing_proof_attachments(state=state, attachments=message.attachments)
                    result = self._build_billing_proof_identity_result(
                        state=state,
                        pending_message=state.metadata.get("pending_message"),
                    )
                else:
                    turn_decision = await self._interpret_pending_identity_turn(
                        state=state,
                        message=message,
                        pending_agent=pending_agent,
                    )
                    if turn_decision.action == "switch_intent":
                        override = await self._handle_pending_identity_switch(
                            state=state,
                            message=message,
                            target_intent=turn_decision.target_intent,
                        )
                        if override is not None:
                            decision, result = override
                        else:
                            result = self._build_identity_request_result(
                                pending_agent=pending_agent,
                                pending_message=state.metadata.get("pending_message"),
                            )
                    elif str((turn_decision.slot_updates or {}).get("identity_status") or "").strip().lower() == "missing":
                        result = await self._build_missing_identity_result(
                            message=message,
                            state=state,
                            pending_agent=pending_agent,
                        )
                    elif str((turn_decision.slot_updates or {}).get("identity_status") or "").strip().lower() == "holder_name":
                        result = self._build_identity_name_clarification_result(
                            pending_agent=pending_agent,
                            pending_message=state.metadata.get("pending_message"),
                        )
                    elif self._user_reports_missing_identity(message.mensaje):
                        result = await self._build_missing_identity_result(
                            message=message,
                            state=state,
                            pending_agent=pending_agent,
                        )
                    elif self._user_sent_holder_name_instead_of_document(message.mensaje):
                        result = self._build_identity_name_clarification_result(
                            pending_agent=pending_agent,
                            pending_message=state.metadata.get("pending_message"),
                        )
                    else:
                        result = self._build_identity_request_result(
                            pending_agent=pending_agent,
                            pending_message=state.metadata.get("pending_message"),
                        )
            elif (
                sales_state.get("awaiting_crm_field")
                or sales_state.get("awaiting_agency_location")
                or sales_state.get("pending_intent") in {"commercial", "agencies"}
                or (
                    state.current_intent == "commercial"
                    and (
                        sales_state.get("catalog_context")
                        or sales_state.get("selected_catalog_plan")
                        or sales_state.get("recommended_plan")
                    )
                )
            ):
                decision = RouteDecision(
                    agent="sales",
                    intent="sales",
                    confidence=0.99,
                    reason="sales_session_state",
                )
                result = await self.sales.handle(message, state)
            elif (
                contact_state.get("awaiting_contract_selection")
                or contact_state.get("awaiting_consent")
                or (contact_state.get("billing") or {}).get("awaiting_action")
                or (contact_state.get("billing") or {}).get("awaiting_proof")
                or (contact_state.get("billing") or {}).get("processing_async")
                or support_state.get("awaiting_otp")
                or support_state.get("awaiting_credentials")
                or support_state.get("awaiting_issue_type")
                or support_state.get("awaiting_resolution_confirmation")
            ):
                if (
                    support_state.get("awaiting_otp")
                    or support_state.get("awaiting_credentials")
                    or support_state.get("awaiting_issue_type")
                    or support_state.get("awaiting_resolution_confirmation")
                ):
                    pending_agent = "support"
                elif contact_state.get("awaiting_consent"):
                    pending_agent = contact_state.get("consent_pending_domain") or contact_state.get("last_domain") or "support"
                elif (
                    (contact_state.get("billing") or {}).get("awaiting_action")
                    or (contact_state.get("billing") or {}).get("awaiting_proof")
                    or (contact_state.get("billing") or {}).get("processing_async")
                ):
                    pending_agent = "billing"
                else:
                    pending_agent = contact_state.get("last_domain") or "support"
                decision = RouteDecision(
                    agent=pending_agent,
                    intent=pending_agent,
                    confidence=0.99,
                    reason="contact_session_state",
                )
                result = await self._execute(decision, message, state)
            else:
                decision = await self.router.decide(message, state)
                logger.info(
                    "orchestrator_router_decision session_id=%s agent=%s intent=%s confidence=%.2f reason=%s",
                    message.session_id,
                    decision.agent,
                    decision.intent,
                    decision.confidence,
                    decision.reason,
                )
                if should_require_identity(message, state) and decision.agent in {"support", "billing"}:
                    pending_agent = decision.agent
                    decision = RouteDecision(
                        agent="clarify",
                        intent="ask_cedula",
                        confidence=1.0,
                        reason="identity_required",
                        requires_clarification=True,
                    )
                    pending_message = (message.mensaje or "").strip() or None
                    if pending_agent == "billing" and message.attachments:
                        self._cache_pending_billing_proof_attachments(state=state, attachments=message.attachments)
                        result = self._build_billing_proof_identity_result(
                            state=state,
                            pending_message=pending_message,
                        )
                    elif self._user_reports_missing_identity(message.mensaje):
                        result = await self._build_missing_identity_result(
                            message=message,
                            state=state,
                            pending_agent=pending_agent,
                            pending_message=pending_message,
                        )
                    elif self._user_sent_holder_name_instead_of_document(message.mensaje):
                        result = self._build_identity_name_clarification_result(
                            pending_agent=pending_agent,
                            pending_message=pending_message,
                        )
                    else:
                        result = self._build_identity_request_result(
                            pending_agent=pending_agent,
                            pending_message=pending_message,
                        )
                else:
                    result = await self._execute(decision, message, state)

            result = await self.response_composer.compose(
                message=message,
                state=state,
                decision=decision,
                result=result,
            )
            if decision.agent != "handoff" and self._should_direct_handoff_for_openai_outage():
                decision, result = await self._handle_openai_runtime_unavailable(message=message, state=state)
            logger.info(
                "orchestrator_result session_id=%s agent=%s intent=%s decision_reason=%s requires_clarification=%s result_preview=%r",
                message.session_id,
                result.agent,
                result.intent,
                decision.reason,
                decision.requires_clarification,
                " ".join((result.message or "").split())[:160],
            )
            state = update_state(state, decision, result)
            self._remember_inbound_message(message, state)
            if result.intent != "ask_cedula":
                state.metadata.pop("missing_identity_attempts", None)
            await self.sessions.save(state)
            await self._sync_contact_registry(message=message, state=state, decision=decision, result=result)
            outbound = build_outbound_message(decision, result)
            assistant_profile = (outbound.metadata or {}).get("assistant_profile")
            if not isinstance(assistant_profile, dict) or not assistant_profile:
                state_profile = (state.metadata or {}).get("assistant_profile")
                if isinstance(state_profile, dict) and state_profile:
                    assistant_profile = state_profile
            merged_metadata = dict(outbound.metadata or {})
            if isinstance(assistant_profile, dict) and assistant_profile:
                merged_metadata["assistant_profile"] = assistant_profile
            audio_meta = (message.metadata or {}).get("audio")
            if isinstance(audio_meta, dict) and audio_meta:
                merged_metadata["audio"] = audio_meta
            if merged_metadata != (outbound.metadata or {}):
                outbound = outbound.model_copy(update={"metadata": merged_metadata})
            logger.info(
                "orchestrator_done session_id=%s saved_intent=%s last_agent=%s awaiting_field=%s human_handoff=%s",
                message.session_id,
                state.current_intent,
                state.last_agent,
                state.awaiting_field,
                state.human_handoff,
            )
            return outbound
        except Exception as exc:
            return await self._handle_system_failure(message=message, state=state, exc=exc)

    async def _maybe_preprocess_audio_message(self, message: InboundMessage) -> InboundMessage:
        """Si el mensaje trae audio, intenta transcribirlo antes de seguir con el ruteo."""
        if not self.settings.audio_enabled:
            return message
        audio_attachment = next((item for item in (message.attachments or []) if getattr(item, "type", None) == "audio"), None)
        if audio_attachment is None:
            return message

        result = await self.speech_to_text.transcribe_attachment(audio_attachment)
        audio_meta = {
            "input": True,
            "stt_status": result.get("status"),
            "stt_engine": result.get("engine"),
            "mime_type": result.get("mime_type"),
            "filename": result.get("filename"),
        }
        transcript = " ".join(str(result.get("text") or "").split()).strip()
        if result.get("duration") is not None:
            audio_meta["duration"] = result.get("duration")
        if result.get("language"):
            audio_meta["language"] = result.get("language")
        if result.get("status") == "ok" and transcript:
            audio_meta["transcribed"] = True
            audio_meta["transcript"] = transcript
            logger.info(
                "orchestrator_audio_transcribed session_id=%s recipient=%s preview=%r",
                message.session_id,
                message.recipient,
                " ".join(transcript.split())[:160],
            )
            return message.model_copy(
                update={
                    "mensaje": transcript,
                    "metadata": {**(message.metadata or {}), "audio": audio_meta},
                }
            )

        audio_meta["transcribed"] = False
        logger.info(
            "orchestrator_audio_transcription_unavailable session_id=%s recipient=%s status=%s",
            message.session_id,
            message.recipient,
            result.get("status"),
        )
        return message.model_copy(update={"metadata": {**(message.metadata or {}), "audio": audio_meta}})

    async def _handle_system_failure(
        self,
        *,
        message: InboundMessage,
        state: SessionState | None,
        exc: Exception,
    ) -> OutboundMessage:
        """Maneja system failure y avanza el flujo."""
        logger.exception(
            "orchestrator_unhandled_error session_id=%s channel=%s recipient=%s",
            message.session_id,
            message.channel,
            message.recipient,
        )
        if state is None:
            state = SessionState(
                session_id=message.session_id,
                channel=message.channel,
                recipient=message.recipient,
                cedula=message.cedula,
            )
        summary = (
            "Fallo interno durante la atención automatizada. "
            f"session_id={message.session_id}. "
            f"canal={message.channel}. "
            f"recipient={message.recipient}. "
            f"error={exc.__class__.__name__}: {str(exc)[:240]}"
        )
        decision = RouteDecision(
            agent="clarify",
            intent="clarify",
            confidence=1.0,
            reason="system_error",
        )
        result = await self._create_direct_handoff_result(
            message=message,
            state=state,
            summary=summary,
            group=self.handoff._handoff_group(state),
            origen=self.handoff._handoff_origen(state),
            final_message=(
                format_system_error_handoff_message()
            ),
            failure_message=(
                format_system_error_handoff_failure_message()
            ),
            failure_conversation_state="system_handoff_failed",
            failure_hypothesis="system_error_and_handoff_failed",
            failure_next_step="retry_later",
            error_type=exc.__class__.__name__,
        )
        if result.agent == "handoff":
            decision = RouteDecision(
                agent="handoff",
                intent="human_handoff",
                confidence=1.0,
                reason="system_error",
            )
        try:
            result = await self.response_composer.compose(
                message=message,
                state=state,
                decision=decision,
                result=result,
            )
        except Exception:
            logger.exception("orchestrator_system_failure_compose_failed session_id=%s", message.session_id)
        state = update_state(state, decision, result)
        try:
            await self.sessions.save(state)
        except Exception:
            logger.exception("orchestrator_system_failure_save_failed session_id=%s", message.session_id)
        try:
            await self._sync_contact_registry(message=message, state=state, decision=decision, result=result)
        except Exception:
            logger.exception("orchestrator_system_failure_registry_failed session_id=%s", message.session_id)
        logger.info(
            "orchestrator_done session_id=%s saved_intent=%s last_agent=%s awaiting_field=%s human_handoff=%s",
            message.session_id,
            state.current_intent,
            state.last_agent,
            state.awaiting_field,
            state.human_handoff,
        )
        return build_outbound_message(decision, result)

    def _should_direct_handoff_for_openai_outage(self) -> bool:
        """Decide si conviene mandar el caso directo a un humano cuando OpenAI falla."""
        if not bool(getattr(self.settings, "openai_human_handoff_on_runtime_failure", True)):
            return False
        return OpenAIClient.runtime_unavailable()

    async def _handle_openai_runtime_unavailable(
        self,
        *,
        message: InboundMessage,
        state: SessionState,
    ) -> tuple[RouteDecision, AgentResult]:
        """Genera una ruta segura cuando el runtime de OpenAI no está disponible."""
        heuristic_decision = await self.router.heuristic_decide(message, state)
        group = "iainfo" if heuristic_decision.agent == "sales" else "support"
        origen = self.settings.info_origen if group == "iainfo" else "ia"
        failure_detail = str(OpenAIClient.runtime_failure_detail() or "openai_runtime_unavailable").strip()
        summary = (
            "Fallback a asesor especializado por indisponibilidad temporal de OpenAI. "
            f"session_id={message.session_id}. "
            f"dominio_inferido={heuristic_decision.agent}. "
            f"recipient={message.recipient}. "
            f"mensaje_usuario={(message.mensaje or '').strip()[:240]!r}. "
            f"error={failure_detail[:240]}"
        )
        result = await self._create_direct_handoff_result(
            message=message,
            state=state,
            summary=summary,
            group=group,
            origen=origen,
            final_message=format_openai_runtime_handoff_message(),
        )
        if result.agent == "handoff":
            decision = RouteDecision(
                agent="handoff",
                intent="human_handoff",
                confidence=1.0,
                reason="openai_runtime_unavailable",
            )
        else:
            decision = RouteDecision(
                agent="clarify",
                intent="clarify",
                confidence=1.0,
                reason="openai_runtime_unavailable_handoff_failed",
                requires_clarification=True,
            )
        return decision, result

    async def _create_direct_handoff_result(
        self,
        *,
        message: InboundMessage,
        state: SessionState,
        summary: str,
        group: str,
        origen: str,
        final_message: str,
        failure_message: str | None = None,
        failure_conversation_state: str = "handoff_failed",
        failure_hypothesis: str = "handoff_failed",
        failure_next_step: str = "retry_later",
        error_type: str | None = None,
    ) -> AgentResult:
        """Devuelve el resultado create direct handoff."""
        try:
            handoff_result = await self.handoff.odoo.escalate_new_client(
                channel=message.channel,
                recipient=message.recipient,
                summary=summary,
                cedula=message.cedula or state.cedula,
                origen=origen,
                group=group,
            )
        except Exception as exc:
            logger.exception(
                "orchestrator_direct_handoff_failed session_id=%s recipient=%s group=%s",
                message.session_id,
                message.recipient,
                group,
            )
            failed_message = failure_message or (
                "Estoy teniendo un inconveniente temporal y no pude derivarte con un asesor especializado en este momento. "
                "Por favor vuelve a escribir en unos minutos."
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
            return AgentResult(
                message=failed_message,
                intent="clarify",
                agent="clarify",
                metadata=response_plan_metadata(
                    response_plan,
                    {
                        "handoff_failed": True,
                        "handoff_error": f"{exc.__class__.__name__}: {exc}",
                        "handoff_group": group,
                        "handoff_origen": origen,
                        "error_type": error_type or exc.__class__.__name__,
                    },
                ),
            )
        if message.attachments:
            try:
                await self.handoff.odoo.relay_attachments(
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
                    "orchestrator_direct_handoff_attachment_relay_failed session_id=%s recipient=%s group=%s",
                    message.session_id,
                    message.recipient,
                    group,
                )
        response_plan = build_handoff_response_plan(
            message=final_message,
            conversation_state="handoff_created",
            reply_goal="confirmar con calma que el caso ya quedó con un asesor especializado",
            summary=summary,
            target_group=group,
            hypothesis="direct_handoff_created",
            next_step="wait_human_followup",
            should_handoff=True,
            handoff_reason=summary,
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

    @staticmethod
    def _hydrate_identity(message: InboundMessage, state) -> InboundMessage:
        """Completa la identidad del cliente con datos que ya estén disponibles en el mensaje o la sesión."""
        if message.cedula:
            return message
        document = extract_identity_document(message.mensaje)
        if document:
            return message.model_copy(update={"cedula": document})
        if state.cedula:
            return message.model_copy(update={"cedula": state.cedula})
        return message

    async def _execute(self, decision: RouteDecision, message: InboundMessage, state) -> AgentResult:
        """Ejecuta la operacion remota usando la configuracion actual."""
        if decision.agent == "support":
            return await self.support.handle(message, state)
        if decision.agent == "billing":
            return await self.billing.handle(message, state)
        if decision.agent == "sales":
            return await self.sales.handle(message, state)
        if decision.agent == "handoff":
            return await self.handoff.handle(message, state)
        return self._build_clarify_result(decision.reason, state, user_message=message.mensaje)

    @staticmethod
    def _inbound_message_ids(message: InboundMessage) -> list[str]:
        """Devuelve el ids inbound mensaje."""
        metadata = message.metadata or {}
        ids = []
        primary = str(metadata.get("message_id") or "").strip()
        if primary:
            ids.append(primary)
        for item in metadata.get("coalesced_message_ids") or []:
            normalized = str(item or "").strip()
            if normalized and normalized not in ids:
                ids.append(normalized)
        return ids

    def _is_duplicate_inbound(self, message: InboundMessage, state: SessionState) -> bool:
        """Indica si inbound duplicate se cumple."""
        message_ids = self._inbound_message_ids(message)
        if not message_ids:
            return False
        recent = state.metadata.get("recent_inbound_message_ids") or []
        return any(message_id in recent for message_id in message_ids)

    def _remember_inbound_message(self, message: InboundMessage, state: SessionState) -> None:
        """Devuelve el mensaje remember inbound."""
        message_ids = self._inbound_message_ids(message)
        if not message_ids:
            return
        recent = [str(item).strip() for item in (state.metadata.get("recent_inbound_message_ids") or []) if str(item).strip()]
        for message_id in message_ids:
            recent = [item for item in recent if item != message_id]
            recent.append(message_id)
        state.metadata["recent_inbound_message_ids"] = recent[-20:]

    @staticmethod
    def _normalize_conversation_text(text: str | None) -> str:
        """Normaliza texto conversation."""
        normalized = unicodedata.normalize("NFKD", str(text or ""))
        ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
        return " ".join(ascii_value.split())

    @staticmethod
    def _recent_turns(state: SessionState) -> list[dict[str, str]]:
        """Devuelve los turnos recientes de la conversacion."""
        recent_turns = []
        for item in (state.history or [])[-6:]:
            role = str((item or {}).get("role") or "").strip().lower()
            content = str((item or {}).get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                recent_turns.append({"role": role, "content": content[:240]})
        return recent_turns

    @staticmethod
    def _last_assistant_turn(state: SessionState) -> str | None:
        """Devuelve el turno last assistant."""
        for item in reversed(state.history or []):
            if str((item or {}).get("role") or "").strip().lower() != "assistant":
                continue
            content = str((item or {}).get("content") or "").strip()
            if content:
                return content[:320]
        return None

    async def _interpret_pending_identity_turn(
        self,
        *,
        state: SessionState,
        message: InboundMessage,
        pending_agent: str,
    ) -> FlowTurnInterpretation:
        """Interpreta pending identity turn."""
        fallback = FlowTurnInterpretation(action="unclear", reason="identity_turn_fallback")
        sales_analysis = analyze_sales_message(message.mensaje)
        if sales_analysis.routes_to_sales_classifier:
            fallback = FlowTurnInterpretation(
                action="switch_intent",
                target_intent="sales",
                reason="identity_turn_sales_request",
            )
        elif self._user_reports_missing_identity(message.mensaje):
            fallback = FlowTurnInterpretation(
                action="answer_current_step",
                reason="identity_missing",
                slot_updates={"identity_status": "missing"},
            )
        elif self._user_sent_holder_name_instead_of_document(message.mensaje):
            fallback = FlowTurnInterpretation(
                action="answer_current_step",
                reason="identity_holder_name",
                slot_updates={"identity_status": "holder_name"},
            )
        interpretation = await self.turn_interpreter.interpret(
            flow_name="orchestrator_identity",
            current_stage="identity_request",
            user_message=message.mensaje,
            current_question=self._last_assistant_turn(state) or format_contract_holder_identity_request(),
            allowed_actions=("answer_current_step", "switch_intent", "unclear"),
            allowed_switch_intents=("support", "billing", "sales", "human_handoff"),
            known_state={
                "pending_agent": pending_agent,
                "awaiting_field": state.awaiting_field,
                "pending_message": state.metadata.get("pending_message"),
                "slot_hints": {
                    "identity_status": "missing | holder_name",
                },
            },
            recent_turns=self._recent_turns(state),
            initial_analysis={"pending_agent": pending_agent},
            fallback=fallback,
        )
        logger.info(
            "orchestrator_identity_turn session_id=%s pending_agent=%s action=%s reason=%s slots=%s",
            state.session_id,
            pending_agent,
            interpretation.action,
            interpretation.reason,
            interpretation.slot_updates,
        )
        return interpretation

    async def _handle_pending_identity_switch(
        self,
        *,
        state: SessionState,
        message: InboundMessage,
        target_intent: str | None,
    ) -> tuple[RouteDecision, AgentResult] | None:
        """Maneja el caso en que el cliente cambia de tema mientras se pide identidad."""
        target = str(target_intent or "").strip().lower()
        if target == "sales":
            decision = RouteDecision(
                agent="sales",
                intent="sales",
                confidence=0.9,
                reason="contextual_identity_switch",
            )
            return decision, await self.sales.handle(message, state)
        if target == "human_handoff":
            decision = RouteDecision(
                agent="handoff",
                intent="human_handoff",
                confidence=0.95,
                reason="contextual_identity_switch",
            )
            return decision, await self.handoff.handle(message, state)
        if target in {"support", "billing"}:
            return (
                RouteDecision(
                    agent="clarify",
                    intent="ask_cedula",
                    confidence=0.92,
                    reason="contextual_identity_switch",
                    requires_clarification=True,
                ),
                self._build_identity_request_result(
                    pending_agent=target,
                    pending_message=state.metadata.get("pending_message"),
                ),
            )
        return None

    def _build_identity_request_result(
        self,
        *,
        pending_agent: str,
        pending_message: str | None = None,
    ) -> AgentResult:
        """Construye resultado identity request a partir del contexto disponible."""
        text = format_contract_holder_identity_request()
        response_plan = build_clarify_response_plan(
            message=text,
            conversation_state="identity_request",
            reply_goal="pedir el documento del titular sin sonar burocrático",
            hypothesis="missing_contract_holder_identity",
            next_step="request_cedula_or_ruc",
            pending_agent=pending_agent,
            pending_message=pending_message,
        )
        return AgentResult(
            message=text,
            intent="ask_cedula",
            agent="clarify",
            metadata=response_plan_metadata(
                response_plan,
                {
                    "pending_agent": pending_agent,
                    "pending_message": pending_message,
                },
            ),
        )

    def _user_reports_missing_identity(self, text: str | None) -> bool:
        """Devuelve el identity user reports missing."""
        lowered = self._normalize_conversation_text(text)
        if not lowered:
            return False
        compact = re.sub(r"[^a-z0-9]", "", lowered)
        markers = (
            "no se",
            "no se eso",
            "no recuerdo",
            "no me acuerdo",
            "ni idea",
            "se me olvido",
            "se me olvidó",
            "no la tengo",
            "no lo tengo",
            "no la se",
            "no lo se",
            "no la recuerdo",
            "no lo recuerdo",
            "no recuerdo mi cedula",
            "no recuerdo la cedula",
            "no me acuerdo de mi cedula",
            "no me acuerdo de la cedula",
            "no se mi cedula",
            "no se la cedula",
            "no tengo mi cedula",
            "no tengo la cedula",
            "no cuento con la cedula",
            "no dispongo de la cedula",
            "no tengo mi ruc",
            "no tengo el ruc",
            "no recuerdo mi ruc",
            "no recuerdo el ruc",
            "no se mi ruc",
            "no se el ruc",
        )
        compact_markers = {
            "nose",
            "norecuerdo",
            "nomeacuerdo",
            "niidea",
            "semeolvido",
            "notengo",
            "nolatengo",
            "nolotengo",
            "nolase",
            "nolose",
            "nosemicedula",
            "noselacedula",
            "notengolacedula",
            "norecuerdolacedula",
            "nosemiruc",
            "noseelruc",
            "notengoelruc",
            "norecuerdoelruc",
        }
        return any(marker in lowered for marker in markers) or compact in compact_markers

    def _user_sent_holder_name_instead_of_document(self, text: str | None) -> bool:
        """Devuelve el document user sent holder nombre instead of."""
        raw = str(text or "").strip()
        lowered = self._normalize_conversation_text(raw)
        if not raw or not lowered:
            return False
        if analyze_sales_message(raw).routes_to_sales_classifier:
            return False
        if extract_identity_document(raw):
            return False
        if any(char.isdigit() for char in raw):
            return False
        blocked_tokens = {
            "hola",
            "ayuda",
            "internet",
            "servicio",
            "soporte",
            "facturacion",
            "facturación",
            "imagen",
            "enviada",
            "enviado",
            "documento",
            "archivo",
            "adjunto",
            "audio",
            "plan",
            "planes",
            "cedula",
            "cédula",
            "ruc",
            "contrato",
            "titular",
            "asesor",
            "pago",
            "pague",
            "comprobante",
            "link",
            "cobro",
            "wifi",
            "onu",
            "router",
        }
        if any(token in lowered for token in blocked_tokens):
            return False
        words = re.findall(r"[A-Za-zÁÉÍÓÚáéíóúÑñ]+", raw)
        if not 2 <= len(words) <= 5:
            return False
        significant = [word for word in words if len(word) >= 3]
        if len(significant) < 2:
            return False
        return True

    def _user_declines_assistance(self, text: str | None) -> bool:
        """Devuelve el assistance user declines."""
        lowered = self._normalize_conversation_text(text)
        if not lowered:
            return False
        exact = {
            "no",
            "nop",
            "no gracias",
            "ninguno",
            "nada",
            "no quiero nada",
            "solo estaba viendo",
            "solo probando",
        }
        if lowered in exact:
            return True
        markers = (
            "no necesito nada",
            "no quiero nada",
            "no deseo nada",
            "solo estaba consultando",
            "solo estaba viendo",
            "solo queria probar",
            "solo quería probar",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _assistant_intro_already_sent(state) -> bool:
        """Comprueba si ya se presentó el asistente en esta sesión."""
        if state is None:
            return False
        return bool((state.metadata or {}).get("assistant_intro_sent"))

    def _assistant_intro_prefix(self, state, assistant_name: str) -> str:
        """Devuelve el prefix assistant intro."""
        if self._assistant_intro_already_sent(state):
            return ""
        if state is not None:
            state.metadata["assistant_intro_sent"] = True
        assistant_profile = (state.metadata or {}).get("assistant_profile") if state is not None else None
        return assistant_intro_prefix(assistant_name=assistant_name, assistant_profile=assistant_profile)

    @staticmethod
    def _serialize_pending_attachment(attachment) -> dict | None:
        """Devuelve el adjunto serialize pending."""
        raw = attachment.model_dump() if hasattr(attachment, "model_dump") else attachment
        if not isinstance(raw, dict):
            return None
        mime_type = str(raw.get("mime_type") or "").strip() or None
        filename = str(raw.get("filename") or "").strip() or None
        attachment_type = str(raw.get("type") or "").strip() or None
        base64_data = str(raw.get("base64_data") or "").strip()
        direct_url = str(raw.get("url") or "").strip()
        if base64_data and not direct_url:
            token = store_temp_base64_media(base64_data, mime_type, filename=filename)
            if token:
                direct_url = build_public_media_url(token)
        if not direct_url and not base64_data:
            return None
        return {
            "type": attachment_type,
            "mime_type": mime_type,
            "filename": filename,
            "url": direct_url or None,
            "base64_data": base64_data or None,
        }

    def _cache_pending_billing_proof_attachments(self, *, state, attachments) -> None:
        """Devuelve los adjuntos cache pending billing proof."""
        contact_state = state.metadata.setdefault("contact", {})
        billing_state = contact_state.setdefault("billing", {})
        serialized = []
        for item in attachments or []:
            prepared = self._serialize_pending_attachment(item)
            if prepared:
                serialized.append(prepared)
        if serialized:
            billing_state["pending_proof_attachments"] = serialized

    def _build_billing_proof_identity_result(
        self,
        *,
        state,
        pending_message: str | None = None,
    ) -> AgentResult:
        """Construye resultado billing proof identity a partir del contexto disponible."""
        assistant_name = self._assistant_name_for_state(state)
        intro = self._assistant_intro_prefix(state, assistant_name)
        text = format_billing_proof_identity_request(intro=intro)
        response_plan = build_clarify_response_plan(
            message=text,
            conversation_state="billing_proof_identity_request",
            reply_goal="pedir el documento del titular para revisar el comprobante sin cortar la conversación",
            hypothesis="payment_proof_needs_contract_holder_identity",
            next_step="request_cedula_or_ruc",
            pending_agent="billing",
            pending_message=pending_message or "Adjunto comprobante",
        )
        return AgentResult(
            message=text,
            intent="ask_cedula",
            agent="clarify",
            metadata=response_plan_metadata(
                response_plan,
                {
                    "pending_agent": "billing",
                    "pending_message": pending_message or "Adjunto comprobante",
                },
            ),
        )

    async def _build_missing_identity_result(
        self,
        *,
        message: InboundMessage,
        state,
        pending_agent: str,
        pending_message: str | None = None,
    ) -> AgentResult:
        """Genera la ruta de traspaso cuando el cliente no cuenta con la identidad requerida."""
        effective_pending_message = pending_message
        if effective_pending_message is None:
            effective_pending_message = str(state.metadata.get("pending_message") or "").strip() or None
        attempts = int(state.metadata.get("missing_identity_attempts") or 0) + 1
        state.metadata["missing_identity_attempts"] = attempts

        summary = (
            f"Cliente indica que no cuenta con la cédula o RUC del titular. "
            f"Flujo pendiente: {pending_agent}. Intentos sin documento: {attempts}."
        )
        if effective_pending_message:
            summary += f" Mensaje pendiente: {effective_pending_message}."
        return await self._create_direct_handoff_result(
            message=message,
            state=state,
            summary=summary,
            group=pending_agent,
            origen="ia",
            final_message=SOFT_IDENTITY_HANDOFF_MESSAGE,
            failure_message=(
                "Dame un momento por favor. "
                "Justo ahora no pude dejar su caso con un asesor especializado, pero si lo prefiere vuelva a escribirme en un momento."
            ),
            failure_conversation_state="identity_handoff_failed",
            failure_hypothesis="missing_identity_handoff_failed",
            failure_next_step="retry_handoff_later",
            error_type="missing_identity_handoff_failed",
        )

    def _build_identity_name_clarification_result(
        self,
        *,
        pending_agent: str,
        pending_message: str | None = None,
    ) -> AgentResult:
        """Aclara que se necesita el documento y no solo el nombre del titular."""
        text = format_identity_name_clarification(pending_agent=pending_agent)
        response_plan = build_clarify_response_plan(
            message=text,
            conversation_state="identity_name_instead_of_document",
            reply_goal="aclarar con tacto que hace falta el documento y no el nombre",
            hypothesis="holder_name_instead_of_identity_document",
            next_step="request_cedula_or_ruc",
            pending_agent=pending_agent,
            pending_message=pending_message,
        )
        return AgentResult(
            message=text,
            intent="ask_cedula",
            agent="clarify",
            metadata=response_plan_metadata(
                response_plan,
                {
                    "pending_agent": pending_agent,
                    "pending_message": pending_message,
                },
            ),
        )

    def _build_clarify_result(self, reason: str, state=None, *, user_message: str | None = None) -> AgentResult:
        """Construye resultado clarify a partir del contexto disponible."""
        assistant_profile = self._assistant_profile_for_state(state)
        assistant_name = assistant_profile["display_name"]
        intro = self._assistant_intro_prefix(state, assistant_name)
        normalized_user_message = self._normalize_conversation_text(user_message)
        message = format_clarify_message(
            reason=reason,
            intro=intro,
            assistant_profile=assistant_profile,
            normalized_user_message=normalized_user_message,
            user_declines_assistance=self._user_declines_assistance(normalized_user_message),
        )
        response_plan = build_clarify_response_plan(
            message=message,
            conversation_state=f"clarify_{reason}",
            reply_goal="mantener una aclaración conversacional y orientar el siguiente paso",
            hypothesis=reason,
            next_step="wait_for_clearer_request" if reason not in {"greeting_only", "small_talk"} else "invite_supported_request",
            pending_message=user_message,
        )
        return AgentResult(
            message=message,
            intent="clarify",
            agent="clarify",
            metadata=response_plan_metadata(response_plan, {"assistant_profile": assistant_profile}),
        )

    def _assistant_name_for_state(self, state) -> str:
        """Devuelve el estado assistant nombre for."""
        return self._assistant_profile_for_state(state)["display_name"]

    def _assistant_profile_for_state(self, state) -> dict[str, str]:
        """Devuelve el estado assistant perfil for."""
        if state is None:
            display_name = self.settings.assistant_name
            profile = {"display_name": display_name}
            style = ensure_assistant_greeting_style(profile)
            voice_gender = self._infer_voice_gender(display_name)
            profile["greeting_style"] = style["id"]
            if voice_gender:
                profile["voice_gender"] = voice_gender
            return profile
        profile = state.metadata.setdefault("assistant_profile", {})
        chosen = str(profile.get("display_name") or "").strip()
        if not chosen:
            names = [
                item.strip()
                for item in str(self.settings.assistant_names or "").split(",")
                if item.strip()
            ]
            chosen = pick_stable_option(
                tuple(names),
                seed=f"{state.session_id}:assistant_name",
            ) or self.settings.assistant_name
            profile["display_name"] = chosen
        style = ensure_assistant_greeting_style(
            profile,
            seed=f"{state.session_id}:greeting_style",
        )
        voice_gender = str(profile.get("voice_gender") or "").strip().lower()
        if voice_gender not in {"female", "male"}:
            inferred_gender = self._infer_voice_gender(chosen)
            if inferred_gender:
                profile["voice_gender"] = inferred_gender
                voice_gender = inferred_gender
        response = {"display_name": chosen, "greeting_style": style["id"]}
        if voice_gender in {"female", "male"}:
            response["voice_gender"] = voice_gender
        return response

    @staticmethod
    def _infer_voice_gender(display_name: str) -> str | None:
        """Infere una voz de referencia a partir del nombre del asistente."""
        token = str(display_name or "").strip().split(" ", 1)[0].lower()
        if not token:
            return None
        female_names = {
            "andrea",
            "camila",
            "daniela",
            "karla",
            "lorena",
            "maria",
            "paola",
            "salome",
            "sofia",
            "valeria",
        }
        male_names = {
            "alex",
            "andres",
            "carlos",
            "daniel",
            "diego",
            "gonzalo",
            "jorge",
            "juan",
            "kevin",
            "luis",
            "mateo",
            "roberto",
            "victor",
        }
        if token in female_names:
            return "female"
        if token in male_names:
            return "male"
        if token.endswith("a"):
            return "female"
        return None

    @staticmethod
    def _infer_contact_group(*, decision: RouteDecision, state) -> str:
        """Devuelve el grupo infer contact."""
        intent = str(decision.intent or state.current_intent or "").lower()
        agent = str(decision.agent or state.last_agent or "").lower()
        if agent == "sales" or intent in {"sales", "commercial", "agencies"}:
            return "iainfo"
        return "support"

    async def _sync_contact_registry(
        self,
        *,
        message: InboundMessage,
        state,
        decision: RouteDecision,
        result: AgentResult,
    ) -> None:
        """Devuelve el registry sync contact."""
        group = self._infer_contact_group(decision=decision, state=state)
        if result.agent == "handoff" or state.human_handoff:
            await self.contact_registry.mark_human_active(
                recipient=message.recipient,
                red=message.channel,
                identificacion=state.cedula or message.cedula,
                session_id=state.session_id,
                group=group,
                chat_preview=(result.message or "").strip() or None,
            )
            return
        await self.contact_registry.mark_ai_active(
            recipient=message.recipient,
            red=message.channel,
            identificacion=state.cedula or message.cedula,
            session_id=state.session_id,
            group=group,
            chat_preview=(result.message or "").strip() or None,
        )
