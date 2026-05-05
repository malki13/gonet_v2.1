"""ayudas transversales del flujo de contacto."""

import logging

import httpx

from packages.agents.contact_support import CONTACT_FLOW_EXTERNAL_ERRORS
from packages.agents.contact_utils import (
    format_billing_action_nudge,
    format_billing_proof_nudge,
    format_contract_selection,
    format_information_consent_prompt,
    extract_contracts_from_info,
    match_contract_in_text,
    normalize_billing_action,
    user_accepts_information,
)
from packages.agents.contact_support_utils import (
    classify_support_issue,
    extract_support_followup_observations,
    is_affirmative,
    is_negative,
    parse_edit_redes_payload,
    user_reports_missing_otp,
    user_requests_current_network_names,
)
from packages.agents.support_copy import format_support_issue_nudge, format_support_issue_triage_reply
from packages.shared.response_planner import (
    build_clarify_response_plan,
    build_handoff_response_plan,
    response_plan_metadata,
)
from packages.shared.schemas import AgentResult, Attachment, FlowTurnInterpretation, InboundMessage, SessionState

logger = logging.getLogger("contact_flow")


class ContactFlowHelperMixin:
    """Agrupa utilidades transversales del flujo de contacto."""
    ACTIVE_CONTACT_ALLOWED_ACTIONS = {
        "contract_selection": ("answer_current_step", "unclear"),
        "consent": ("accept_information", "unclear"),
        "billing_action": ("answer_current_step", "unclear"),
        "billing_proof": ("answer_current_step", "unclear"),
        "support_issue_type": ("answer_current_step", "unclear"),
        "support_resolution_confirmation": ("answer_current_step", "unclear"),
        "support_otp": ("answer_current_step", "unclear"),
        "support_credentials": ("answer_current_step", "unclear"),
    }

    @staticmethod
    def _state(state: SessionState) -> dict:
        """Devuelve y normaliza el estado persistido que usa este flujo."""
        contact_state = state.metadata.get("contact") or {}
        contact_state.setdefault("contracts", [])
        contact_state.setdefault("selected_contract", None)
        contact_state.setdefault("last_domain", None)
        contact_state.setdefault("contract_lookup_attempts", 0)
        contact_state.setdefault("awaiting_contract_selection", False)
        contact_state.setdefault("awaiting_consent", False)
        contact_state.setdefault("consent_accepted", False)
        contact_state.setdefault("consent_pending_domain", None)
        contact_state.setdefault("consent_pending_message", None)
        billing_state = contact_state.setdefault(
            "billing",
            {
                "awaiting_action": False,
                "awaiting_proof": False,
                "proof_attempts": 0,
                "proof_failures": [],
                "processing_async": False,
                "pending_proof_attachments": [],
            },
        )
        billing_state.setdefault("awaiting_action", False)
        billing_state.setdefault("awaiting_proof", False)
        billing_state.setdefault("proof_attempts", 0)
        billing_state.setdefault("proof_failures", [])
        billing_state.setdefault("processing_async", False)
        billing_state.setdefault("pending_proof_attachments", [])
        support_state = contact_state.setdefault(
            "support",
            {
                "awaiting_otp": False,
                "awaiting_credentials": False,
                "awaiting_issue_type": False,
                "awaiting_resolution_confirmation": False,
                "last_issue_type": None,
                "last_system_issue": None,
                "last_diagnostic": None,
                "last_response_plan": None,
                "last_followup_prompt": None,
                "guided_followup_attempts": 0,
                "manual_checks_requested": False,
                "manual_checks_confirmed": False,
                "pending_contract": None,
            },
        )
        support_state.setdefault("awaiting_otp", False)
        support_state.setdefault("awaiting_credentials", False)
        support_state.setdefault("awaiting_issue_type", False)
        support_state.setdefault("awaiting_resolution_confirmation", False)
        support_state.setdefault("last_issue_type", None)
        support_state.setdefault("last_system_issue", None)
        support_state.setdefault("last_diagnostic", None)
        support_state.setdefault("last_response_plan", None)
        support_state.setdefault("last_followup_prompt", None)
        support_state.setdefault("guided_followup_attempts", 0)
        support_state.setdefault("manual_checks_requested", False)
        support_state.setdefault("manual_checks_confirmed", False)
        support_state.setdefault("pending_contract", None)
        state.metadata["contact"] = contact_state
        return contact_state

    @staticmethod
    def _active_contact_stage(contact_state: dict) -> str | None:
        """Devuelve el paso active contact."""
        if contact_state.get("awaiting_contract_selection"):
            return "contract_selection"
        if contact_state.get("awaiting_consent"):
            return "consent"
        billing_state = contact_state.get("billing") or {}
        if billing_state.get("awaiting_action"):
            return "billing_action"
        if billing_state.get("awaiting_proof"):
            return "billing_proof"
        support_state = contact_state.get("support") or {}
        if support_state.get("awaiting_issue_type"):
            return "support_issue_type"
        if support_state.get("awaiting_resolution_confirmation"):
            return "support_resolution_confirmation"
        if support_state.get("awaiting_otp"):
            return "support_otp"
        if support_state.get("awaiting_credentials"):
            return "support_credentials"
        return None

    @staticmethod
    def _recent_turns(state: SessionState) -> list[dict[str, str]]:
        """Devuelve los turnos recientes del flujo de contacto."""
        recent_turns: list[dict[str, str]] = []
        for item in (state.history or [])[-6:]:
            role = str((item or {}).get("role") or "").strip().lower()
            content = str((item or {}).get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                recent_turns.append({"role": role, "content": content[:220]})
        return recent_turns

    def _active_contact_question(
        self,
        *,
        contact_state: dict,
        contract: dict | None,
        contracts: list[dict],
        stage: str | None,
    ) -> str | None:
        """Devuelve la pregunta active contact."""
        if stage == "contract_selection":
            return format_contract_selection(contracts)
        if stage == "consent":
            return format_information_consent_prompt(contract)
        if stage == "billing_action":
            return format_billing_action_nudge()
        if stage == "billing_proof":
            return format_billing_proof_nudge()
        if stage == "support_issue_type":
            return format_support_issue_triage_reply(contract or {})
        if stage == "support_resolution_confirmation":
            support_state = contact_state.get("support") or {}
            return str(
                support_state.get("last_followup_prompt")
                or "Indíqueme si después de esa validación el problema se solucionó o si todavía sigue igual."
            )
        if stage == "support_otp":
            return "Revise su correo y envíeme el código OTP para continuar con la edición de redes."
        if stage == "support_credentials":
            return format_support_issue_nudge(contract or {})
        return None

    @staticmethod
    def _identity_request_result(
        *,
        message: str,
        pending_agent: str,
        pending_message: str | None = None,
        extra_metadata: dict | None = None,
        conversation_state: str = "identity_request",
        hypothesis: str = "missing_contract_holder_identity",
    ) -> AgentResult:
        """Devuelve el resultado identity request."""
        response_plan = build_clarify_response_plan(
            message=message,
            conversation_state=conversation_state,
            reply_goal="pedir el documento del titular con tono cercano y claro",
            hypothesis=hypothesis,
            next_step="request_cedula_or_ruc",
            pending_agent=pending_agent,
            pending_message=pending_message,
        )
        metadata = {
            "pending_agent": pending_agent,
            "pending_message": pending_message,
            **dict(extra_metadata or {}),
        }
        return AgentResult(
            message=message,
            intent="ask_cedula",
            agent="clarify",
            metadata=response_plan_metadata(response_plan, metadata),
        )

    @staticmethod
    def _contract_selection_result(*, contracts: list[dict], preferred_domain: str) -> AgentResult:
        """Devuelve el resultado contract selection."""
        message = format_contract_selection(contracts)
        response_plan = build_clarify_response_plan(
            message=message,
            conversation_state="contract_selection",
            reply_goal="ayudar al cliente a elegir rápido el contrato correcto sin sonar a formulario",
            hypothesis="multiple_contracts_found_for_customer",
            next_step="await_contract_selection",
            evidence=[f"Contratos encontrados: {len(contracts)}"],
        )
        return AgentResult(
            message=message,
            intent="contract_selection",
            agent=preferred_domain,
            metadata=response_plan_metadata(response_plan, {"contracts_count": len(contracts)}),
        )

    @staticmethod
    def _consent_result(*, contract: dict, actual_domain: str) -> AgentResult:
        """Devuelve el resultado consent."""
        message = format_information_consent_prompt(contract)
        response_plan = build_clarify_response_plan(
            message=message,
            conversation_state="information_consent",
            reply_goal="pedir la confirmación de uso de información de forma natural y breve",
            hypothesis="need_information_consent_before_continuing",
            next_step="await_information_consent",
            pending_agent=actual_domain,
            evidence=[f"Contrato: {contract.get('code')}" if contract.get("code") else None],
        )
        return AgentResult(
            message=message,
            intent="consent_required",
            agent="clarify",
            actions={"type": "buttons", "buttons": [{"id": "ASISTENCIA_ACEPTO", "title": "ACEPTO"}]},
            metadata=response_plan_metadata(response_plan, {"pending_agent": actual_domain, "contract": contract}),
        )

    def _active_contact_known_state(
        self,
        *,
        contact_state: dict,
        preferred_domain: str,
        contract: dict | None,
        contracts: list[dict],
        stage: str | None,
    ) -> dict:
        """Devuelve el estado active contact known."""
        known_state = {
            "preferred_domain": preferred_domain,
            "selected_contract": contact_state.get("selected_contract"),
            "contracts": [{"code": item.get("code"), "state": item.get("state"), "status_label": item.get("status_label")} for item in (contracts or [])[:6]],
        }
        if contract:
            known_state["contract"] = {
                "code": contract.get("code"),
                "state": contract.get("state"),
                "status_label": contract.get("status_label"),
                "partner_name": contract.get("partner_name"),
            }
        if stage == "billing_action" or stage == "billing_proof":
            known_state["billing_state"] = dict(contact_state.get("billing") or {})
            known_state["slot_hints"] = {"billing_action": "register_payment | payment_link", "proof_submitted": "true"}
        if stage == "contract_selection":
            known_state["slot_hints"] = {"selected_contract": "contract code from the available contracts"}
        if stage == "consent":
            known_state["slot_hints"] = {"consent": "true if the user accepts continuing with the contract information"}
        if stage in {"support_issue_type", "support_resolution_confirmation"}:
            known_state["support_state"] = dict(contact_state.get("support") or {})
            known_state["slot_hints"] = {
                "support_issue": "no_service | intermittence | slow_internet | generic_network | edit_network | human",
                "resolution": "resolved | persists",
                "device_scope": "all_devices | single_device",
                "connection_type": "wifi | ethernet | both",
                "near_router_result": "better | same",
                "tested_near_router": "true if user says already tested close to router",
            }
        if stage == "support_otp":
            known_state["slot_hints"] = {"otp_code": "otp code string", "otp_missing": "true if user says they do not have the code"}
        if stage == "support_credentials":
            known_state["slot_hints"] = {
                "show_current_networks": "true if user asks to see current network names",
                "network_name": "new base wifi name",
                "network_password": "new wifi password",
            }
        return known_state

    def _fallback_active_contact_turn(
        self,
        *,
        stage: str,
        message: InboundMessage,
        state: SessionState,
        contact_state: dict,
        preferred_domain: str,
        contract: dict | None,
        contracts: list[dict],
    ) -> FlowTurnInterpretation:
        """Devuelve el turno fallback active contact."""
        text = str(message.mensaje or "")
        normalized = text.strip()
        if stage == "contract_selection":
            selected = match_contract_in_text(text, contracts)
            if selected:
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="matched_contract_selection",
                    slot_updates={"selected_contract": selected},
                )
            return FlowTurnInterpretation(action="unclear", reason="contract_selection_fallback")
        if stage == "consent":
            if user_accepts_information(text, message.metadata):
                return FlowTurnInterpretation(
                    action="accept_information",
                    reason="consent_acceptance",
                    slot_updates={"consent": True},
                )
            return FlowTurnInterpretation(action="unclear", reason="consent_fallback")
        if stage == "billing_action":
            if message.attachments:
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="billing_attachment_received",
                    slot_updates={"billing_action": "register_payment", "proof_submitted": True},
                )
            billing_action = normalize_billing_action(text)
            if billing_action == "Registrar Pago":
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="billing_register_payment",
                    slot_updates={"billing_action": "register_payment"},
                )
            if billing_action == "Link de Cobro":
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="billing_payment_link",
                    slot_updates={"billing_action": "payment_link"},
                )
            return FlowTurnInterpretation(action="unclear", reason="billing_action_fallback")
        if stage == "billing_proof":
            if message.attachments:
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="billing_proof_received",
                    slot_updates={"proof_submitted": True},
                )
            billing_action = normalize_billing_action(text)
            if billing_action == "Link de Cobro":
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="billing_payment_link",
                    slot_updates={"billing_action": "payment_link"},
                )
            return FlowTurnInterpretation(action="unclear", reason="billing_proof_fallback")
        if stage == "support_issue_type":
            support_issue = classify_support_issue(text)
            if support_issue in {"no_service", "intermittence", "slow_internet", "generic_network", "edit_network", "human"}:
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="support_issue_classified",
                    slot_updates={"support_issue": support_issue},
                )
            return FlowTurnInterpretation(action="unclear", reason="support_issue_type_fallback")
        if stage == "support_resolution_confirmation":
            observations = extract_support_followup_observations(text)
            if is_affirmative(text):
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="support_resolution_affirmative",
                    slot_updates={"resolution": "resolved"},
                )
            if observations:
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="support_resolution_observations",
                    slot_updates=observations,
                )
            if is_negative(text):
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="support_resolution_negative",
                    slot_updates={"resolution": "persists"},
                )
            support_issue = classify_support_issue(text)
            if support_issue in {"no_service", "intermittence", "slow_internet", "generic_network", "edit_network", "human"}:
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="support_resolution_reclassified",
                    slot_updates={"support_issue": support_issue},
                )
            return FlowTurnInterpretation(action="unclear", reason="support_resolution_fallback")
        if stage == "support_otp":
            if user_reports_missing_otp(normalized):
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="support_missing_otp",
                    slot_updates={"otp_missing": True},
                )
            otp_len = self.otp.settings.otp_code_len
            if normalized and len(normalized) == otp_len and all(char in "0123456789ABCDEFabcdef" for char in normalized):
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="support_otp_candidate",
                    slot_updates={"otp_code": normalized},
                )
            return FlowTurnInterpretation(action="unclear", reason="support_otp_fallback")
        if stage == "support_credentials":
            if user_requests_current_network_names(text):
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="support_show_current_networks",
                    slot_updates={"show_current_networks": True},
                )
            network_name, network_password = parse_edit_redes_payload(text)
            if network_name and network_password:
                return FlowTurnInterpretation(
                    action="answer_current_step",
                    reason="support_network_credentials",
                    slot_updates={"network_name": network_name, "network_password": network_password},
                )
            return FlowTurnInterpretation(action="unclear", reason="support_credentials_fallback")
        return FlowTurnInterpretation(action="unclear", reason="no_active_contact_stage")

    @staticmethod
    def _should_trust_contact_fallback(*, stage: str, fallback: FlowTurnInterpretation) -> bool:
        """Indica si fallback trust contact se cumple."""
        if fallback.action == "accept_information":
            return True
        slot_updates = dict(fallback.slot_updates or {})
        if stage == "contract_selection" and slot_updates.get("selected_contract"):
            return True
        if stage in {"billing_action", "billing_proof"} and (
            slot_updates.get("billing_action") or slot_updates.get("proof_submitted")
        ):
            return True
        if stage == "support_issue_type":
            support_issue = str(slot_updates.get("support_issue") or "").strip()
            return support_issue in {"no_service", "intermittence", "slow_internet", "edit_network", "human"}
        if stage == "support_resolution_confirmation" and (
            slot_updates.get("resolution")
            or slot_updates.get("device_scope")
            or slot_updates.get("connection_type")
            or slot_updates.get("near_router_result")
            or slot_updates.get("tested_near_router")
            or slot_updates.get("affected_service")
        ):
            return True
        if stage == "support_otp" and (slot_updates.get("otp_missing") or slot_updates.get("otp_code")):
            return True
        if stage == "support_credentials" and (
            slot_updates.get("show_current_networks")
            or (slot_updates.get("network_name") and slot_updates.get("network_password"))
        ):
            return True
        return False

    async def _interpret_active_contact_turn(
        self,
        *,
        message: InboundMessage,
        state: SessionState,
        preferred_domain: str,
        contract: dict | None,
        contracts: list[dict],
    ) -> FlowTurnInterpretation:
        """Interpreta active contact turn."""
        contact_state = self._state(state)
        stage = self._active_contact_stage(contact_state)
        if not stage:
            return FlowTurnInterpretation(action="unclear", reason="no_active_contact_stage")

        fallback = self._fallback_active_contact_turn(
            stage=stage,
            message=message,
            state=state,
            contact_state=contact_state,
            preferred_domain=preferred_domain,
            contract=contract,
            contracts=contracts,
        )
        if self._should_trust_contact_fallback(stage=stage, fallback=fallback):
            return fallback
        interpreter = getattr(self, "turn_interpreter", None)
        if interpreter is None:
            return fallback

        interpretation = await interpreter.interpret(
            flow_name="contact_flow",
            current_stage=stage,
            user_message=message.mensaje,
            current_question=self._active_contact_question(
                contact_state=contact_state,
                contract=contract,
                contracts=contracts,
                stage=stage,
            ),
            allowed_actions=self.ACTIVE_CONTACT_ALLOWED_ACTIONS.get(stage, ("unclear",)),
            known_state=self._active_contact_known_state(
                contact_state=contact_state,
                preferred_domain=preferred_domain,
                contract=contract,
                contracts=contracts,
                stage=stage,
            ),
            recent_turns=self._recent_turns(state),
            initial_analysis={"preferred_domain": preferred_domain},
            fallback=fallback,
        )
        logger.info(
            "contact_active_turn_decision session_id=%s stage=%s action=%s reason=%s slots=%s",
            state.session_id,
            stage,
            interpretation.action,
            interpretation.reason,
            interpretation.slot_updates,
        )
        return interpretation

    @staticmethod
    def _reset_billing_state(contact_state: dict) -> dict:
        """Reinicia billing state para comenzar de nuevo."""
        billing_state = contact_state.setdefault("billing", {})
        billing_state["awaiting_action"] = False
        billing_state["awaiting_proof"] = False
        billing_state["proof_attempts"] = 0
        billing_state["proof_failures"] = []
        billing_state["processing_async"] = False
        billing_state["pending_proof_attachments"] = []
        return billing_state

    @staticmethod
    def _cache_billing_proof_attachments(contact_state: dict, attachments: list[dict]) -> None:
        """Devuelve los adjuntos cache billing proof."""
        billing_state = contact_state.setdefault("billing", {})
        billing_state["pending_proof_attachments"] = [item for item in attachments if isinstance(item, dict)]

    @staticmethod
    def _restore_billing_proof_attachments(contact_state: dict) -> list[Attachment]:
        """Restaura billing proof attachments previamente guardada."""
        billing_state = contact_state.setdefault("billing", {})
        restored: list[Attachment] = []
        for item in billing_state.get("pending_proof_attachments") or []:
            if not isinstance(item, dict):
                continue
            try:
                restored.append(Attachment(**item))
            except Exception:
                continue
        return restored

    @staticmethod
    def _reset_consent_state(contact_state: dict) -> None:
        """Reinicia consent state para comenzar de nuevo."""
        contact_state["awaiting_consent"] = False
        contact_state["consent_pending_domain"] = None
        contact_state["consent_pending_message"] = None

    @staticmethod
    def _mark_information_consent(contact_state: dict) -> None:
        """Marca information consent con la información confirmada."""
        contact_state["consent_accepted"] = True
        ContactFlowHelperMixin._reset_consent_state(contact_state)

    @staticmethod
    def _extract_pending_user_message(
        *,
        message: InboundMessage,
        state: SessionState,
        just_selected_contract: bool,
    ) -> str | None:
        """Extrae mensaje de pending user."""
        stored = str(state.metadata.get("pending_message") or "").strip()
        if stored:
            return stored
        if just_selected_contract:
            return None
        current = " ".join((message.mensaje or "").split()).strip()
        if not current or user_accepts_information(current, message.metadata):
            return None
        digits = "".join(ch for ch in current if ch.isdigit())
        if digits and current == digits and len(digits) >= 8:
            return None
        return current

    async def _load_contracts(self, state: SessionState, cedula: str) -> dict:
        """Carga contracts."""
        contact_state = self._state(state)
        if contact_state.get("contracts"):
            logger.info(
                "contact_contracts_cached session_id=%s cedula=%s count=%s",
                state.session_id,
                cedula,
                len(contact_state["contracts"]),
            )
            return {"ok": True, "data": contact_state["contracts"], "cached": True}
        result = await self.lookup.info_personal_by_cedula(cedula)
        if result.get("ok"):
            contact_state["contracts"] = extract_contracts_from_info(result)
        logger.info(
            "contact_contracts_loaded session_id=%s cedula=%s ok=%s count=%s cached=%s",
            state.session_id,
            cedula,
            result.get("ok"),
            len(contact_state.get("contracts") or []),
            bool(result.get("cached")),
        )
        return result

    async def _human_handoff(self, message: InboundMessage, summary: str, *, final_message: str | None = None) -> AgentResult:
        """Devuelve el handoff human."""
        logger.info(
            "contact_handoff_requested session_id=%s recipient=%s channel=%s summary=%r",
            message.session_id,
            message.recipient,
            message.channel,
            summary[:220],
        )
        try:
            await self.handoff.escalate_new_client(
                channel=message.channel,
                recipient=message.recipient,
                summary=summary,
                cedula=message.cedula,
                origen="ia",
            )
        except httpx.ReadTimeout:
            logger.warning("contact_handoff_timeout session_id=%s", message.session_id)
            success_message = final_message or "Voy a dejar su caso con un asesor especializado para que continúe con la revisión."
            response_plan = build_handoff_response_plan(
                message=success_message,
                conversation_state="handoff_created",
                reply_goal="mantener la derivacion aunque odoo tarde en responder",
                summary=summary,
                target_group="support",
                hypothesis="advisor_handoff_requested",
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
                        "handoff_group": "support",
                        "handoff_origen": "ia",
                        "handoff_timeout": True,
                    },
                ),
            )
        except CONTACT_FLOW_EXTERNAL_ERRORS:
            logger.exception("contact_handoff_failed session_id=%s", message.session_id)
            failed_message = (
                "Quise dejar su caso con un asesor especializado, pero justo ahora no pude completar esa derivación. "
                "Si lo prefiere, vuelva a escribirme en un momento y lo intento otra vez."
            )
            response_plan = build_handoff_response_plan(
                message=failed_message,
                conversation_state="handoff_failed",
                reply_goal="ser honesto cuando la derivación no pudo completarse",
                summary=summary,
                target_group="support",
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
                    {"summary": summary, "handoff_failed": True, "handoff_group": "support", "handoff_origen": "ia"},
                ),
            )
        success_message = final_message or "Voy a dejar su caso con un asesor especializado para que continúe con la revisión."
        response_plan = build_handoff_response_plan(
            message=success_message,
            conversation_state="handoff_created",
            reply_goal="derivar con calidez y dejar claro que el contexto ya quedó enviado",
            summary=summary,
            target_group="support",
            hypothesis="advisor_handoff_requested",
            next_step="wait_human_followup",
            should_handoff=True,
        )
        return AgentResult(
            message=success_message,
            intent="human_handoff",
            agent="handoff",
            metadata=response_plan_metadata(
                response_plan,
                {"summary": summary, "handoff_group": "support", "handoff_origen": "ia"},
            ),
        )
