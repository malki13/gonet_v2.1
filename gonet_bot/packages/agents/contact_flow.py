"""Flujo compartido para soporte y facturacion por contrato."""

import logging

from packages.agents.contact_flow_helpers import ContactFlowHelperMixin
from packages.agents.contact_utils import (
    find_contract_by_code,
    format_contract_holder_identity_request,
    match_contract_in_text,
    user_cannot_provide_holder_document,
    user_accepts_information,
    user_requests_human,
)
from packages.agents.contact_billing import ContactBillingMixin
from packages.agents.contact_support import ContactSupportMixin
from packages.integrations.billing_registration import BillingRegistrationService
from packages.integrations.ocr_queue import OCRJobQueue
from packages.integrations.contract_lookup import ContractLookupClient
from packages.integrations.onu import ONUClient
from packages.integrations.ocr_service_client import OCRServiceClient
from packages.integrations.odoo_chat import OdooChatClient
from packages.integrations.otp_service import OTPService
from packages.integrations.openai_client import OpenAIClient
from packages.integrations.smarttelcom import SmartTelcomClient
from packages.shared.config import get_settings
from packages.orchestrator.orchestrator_copy import format_human_handoff_identity_request
from packages.shared.schemas import AgentResult, InboundMessage, SessionState
from packages.shared.turn_interpreter import ActiveFlowTurnInterpreter

logger = logging.getLogger("contact_flow")
MAX_CONTRACT_LOOKUP_ATTEMPTS = 2
IDENTITY_HANDOFF_MESSAGE = "Dame un momento por favor. Voy a dejar su caso con un asesor especializado."


class ContactFlowService(ContactFlowHelperMixin, ContactSupportMixin, ContactBillingMixin):
    """Flujo compartido para soporte y facturacion por contrato."""

    def __init__(self, *, llm: OpenAIClient | None = None) -> None:
        """Inicializa el contactflowservice con la configuracion necesaria."""
        self.settings = get_settings()
        self.llm = llm or OpenAIClient()
        self.turn_interpreter = ActiveFlowTurnInterpreter(llm=self.llm, settings=self.settings)
        self.lookup = ContractLookupClient()
        self.handoff = OdooChatClient()
        self.ocr = OCRServiceClient()
        self.ocr_queue = OCRJobQueue()
        self.billing = BillingRegistrationService()
        self.otp = OTPService()
        self.onu = ONUClient()
        self.smart = SmartTelcomClient()

    async def handle(self, *, preferred_domain: str, message: InboundMessage, state: SessionState) -> AgentResult:
        """Orquesta el flujo completo por contrato y dominio preferido."""
        contact_state = self._state(state)
        contact_state["last_domain"] = preferred_domain
        just_selected_contract = False
        logger.info(
            "contact_flow_start session_id=%s domain=%s recipient=%s has_cedula=%s selected_contract=%s preview=%r",
            message.session_id,
            preferred_domain,
            message.recipient,
            bool(message.cedula or state.cedula),
            contact_state.get("selected_contract"),
            " ".join((message.mensaje or "").split())[:160],
        )

        if user_requests_human(message.mensaje):
            cedula = message.cedula or state.cedula
            if cedula:
                summary = "Cliente solicita asesor especializado."
                if contact_state.get("selected_contract"):
                    summary += f" Contrato seleccionado: {contact_state['selected_contract']}."
                return await self._human_handoff(message, summary)
            contact_state["pending_handoff_requested"] = True
            return self._identity_request_result(
                message=format_human_handoff_identity_request(),
                pending_agent="handoff",
                pending_message=str(message.mensaje or "").strip() or None,
                conversation_state="identity_request_handoff",
                hypothesis="human_handoff_requested_without_identity",
            )

        cached_contracts = contact_state.get("contracts") or []
        cedula = message.cedula or state.cedula
        if not cedula and not cached_contracts:
            if user_cannot_provide_holder_document(message.mensaje):
                domain_label = "facturación" if preferred_domain == "billing" else "soporte"
                return await self._human_handoff(
                    message,
                    f"Cliente no cuenta con cédula o RUC del titular durante el flujo de {domain_label}.",
                    final_message=IDENTITY_HANDOFF_MESSAGE,
                )
            return self._identity_request_result(
                message=format_contract_holder_identity_request(),
                pending_agent=preferred_domain,
            )

        if cedula:
            result = await self._load_contracts(state, cedula)
            contracts = contact_state.get("contracts") or []
        else:
            contracts = cached_contracts
            result = {"ok": True, "data": contracts, "cached": True}
            logger.info(
                "contact_contracts_reused session_id=%s selected_contract=%s count=%s",
                message.session_id,
                contact_state.get("selected_contract"),
                len(contracts),
            )

        if contact_state.get("pending_handoff_requested") and cedula:
            contact_state["pending_handoff_requested"] = False
            return await self._human_handoff(
                message,
                (
                    "Cliente solicitó asesor especializado. "
                    f"Cédula recibida: {cedula}."
                ),
            )
        if not contracts:
            attempts = int(contact_state.get("contract_lookup_attempts") or 0) + 1
            contact_state["contract_lookup_attempts"] = attempts
            state.cedula = None
            contact_state["contracts"] = []
            contact_state["selected_contract"] = None
            contact_state["awaiting_contract_selection"] = False
            contact_state["consent_accepted"] = False
            self._reset_consent_state(contact_state)
            detail = "No fue posible obtener la información del contrato desde Odoo."
            if not result.get("ok"):
                detail += f" Error: {result.get('error')}."
            if result.get("ok"):
                if attempts >= MAX_CONTRACT_LOOKUP_ATTEMPTS:
                    domain_label = "facturación" if preferred_domain == "billing" else "soporte"
                    return await self._human_handoff(
                        message,
                        (
                            f"No se encontraron contratos asociados después de {attempts} intentos de cédula "
                            f"durante el flujo de {domain_label}. Última cédula ingresada: {cedula}."
                        ),
                        final_message=IDENTITY_HANDOFF_MESSAGE,
                    )
                return self._identity_request_result(
                    message=format_contract_holder_identity_request(contract_not_found=True),
                    pending_agent=preferred_domain,
                    pending_message=state.metadata.get("pending_message"),
                    extra_metadata={"lookup_detail": detail},
                    conversation_state="identity_contract_not_found",
                    hypothesis="identity_provided_but_no_contracts_found",
                )
            return await self._human_handoff(message, detail)

        contact_state["contract_lookup_attempts"] = 0
        selected_contract = contact_state.get("selected_contract")
        if selected_contract and not find_contract_by_code(contracts, selected_contract):
            selected_contract = None
            contact_state["selected_contract"] = None

        if not selected_contract:
            turn_decision = await self._interpret_active_contact_turn(
                message=message,
                state=state,
                preferred_domain=preferred_domain,
                contract=None,
                contracts=contracts,
            )
            chosen = str((turn_decision.slot_updates or {}).get("selected_contract") or "").strip()
            if not chosen:
                chosen = match_contract_in_text(message.mensaje, contracts)
            if chosen:
                contact_state["selected_contract"] = chosen
                selected_contract = chosen
                just_selected_contract = True
                logger.info("contact_contract_selected session_id=%s contract=%s source=user_match", message.session_id, selected_contract)
            elif len(contracts) > 1:
                contact_state["awaiting_contract_selection"] = True
                logger.info("contact_contract_selection_required session_id=%s count=%s", message.session_id, len(contracts))
                return self._contract_selection_result(contracts=contracts, preferred_domain=preferred_domain)
            else:
                selected_contract = contracts[0]["code"]
                contact_state["selected_contract"] = selected_contract
                logger.info("contact_contract_selected session_id=%s contract=%s source=single_contract", message.session_id, selected_contract)

        contact_state["awaiting_contract_selection"] = False
        contract = find_contract_by_code(contracts, selected_contract)
        if not contract:
            return await self._human_handoff(message, "No pude identificar el contrato del cliente después de la selección.")
        logger.info(
            "contact_contract_ready session_id=%s domain=%s contract=%s state=%s",
            message.session_id,
            preferred_domain,
            contract.get("code"),
            contract.get("status_label") or contract.get("state"),
        )

        actual_domain = "billing" if preferred_domain == "billing" or contract.get("status_label") == "cortado" else "support"
        contact_state["last_domain"] = actual_domain
        if not contact_state.get("consent_accepted"):
            turn_decision = await self._interpret_active_contact_turn(
                message=message,
                state=state,
                preferred_domain=actual_domain,
                contract=contract,
                contracts=contracts,
            )
            if turn_decision.action == "accept_information" or user_accepts_information(message.mensaje, message.metadata):
                pending_domain = contact_state.get("consent_pending_domain") or actual_domain
                pending_message = str(contact_state.get("consent_pending_message") or "").strip()
                self._mark_information_consent(contact_state)
                if pending_domain == "billing":
                    just_selected_contract = True
                elif pending_message:
                    message = message.model_copy(update={"mensaje": pending_message})
                actual_domain = pending_domain
            else:
                contact_state["awaiting_consent"] = True
                contact_state["consent_pending_domain"] = actual_domain
                contact_state["consent_pending_message"] = self._extract_pending_user_message(
                    message=message,
                    state=state,
                    just_selected_contract=just_selected_contract,
                )
                if actual_domain == "billing" and message.attachments:
                    self._cache_billing_proof_attachments(
                        contact_state,
                        [item.model_dump() if hasattr(item, "model_dump") else item for item in message.attachments],
                    )
                return self._consent_result(contract=contract, actual_domain=actual_domain)

        if actual_domain == "billing":
            if not message.attachments:
                cached_proof_attachments = self._restore_billing_proof_attachments(contact_state)
                if cached_proof_attachments:
                    message = message.model_copy(
                        update={
                            "attachments": cached_proof_attachments,
                            "mensaje": str(message.mensaje or "").strip() or "Adjunto comprobante",
                        }
                    )
            return await self._handle_billing(message=message, state=state, contract=contract, just_selected_contract=just_selected_contract)
        return await self._handle_support(message=message, state=state, contract=contract)
