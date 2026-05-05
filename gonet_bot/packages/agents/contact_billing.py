"""flujo de contacto de facturacion."""

import logging
import uuid

import httpx

from packages.agents.contact_utils import (
    describe_billing_proof_failure,
    format_billing_async_retry_message,
    format_billing_action_nudge,
    format_billing_duplicate_message,
    format_billing_handoff_summary,
    format_billing_options,
    format_billing_proof_request,
    format_billing_proof_nudge,
    normalize_billing_action,
    payment_link,
)
from packages.shared.response_planner import build_billing_response_plan, response_plan_metadata
from packages.shared.errors import OCRQueueUnavailableError
from packages.shared.schemas import AgentResult, InboundMessage, OCRJob, SessionState

logger = logging.getLogger("contact_flow")
CONTACT_FLOW_EXTERNAL_ERRORS = (httpx.HTTPError, RuntimeError, ValueError)


class ContactBillingMixin:
    """Agrupa la lógica de facturación dentro del flujo de contacto."""
    
    @staticmethod
    def _billing_response_metadata(
        *,
        contract: dict,
        message: str,
        conversation_state: str,
        reply_goal: str,
        hypothesis: str | None = None,
        next_step: str | None = None,
        followup_prompt: str | None = None,
        should_handoff: bool = False,
        handoff_reason: str | None = None,
        proof_attempts: int | None = None,
        ocr_status: str | None = None,
        reconnect_status: str | None = None,
        extra: dict | None = None,
    ) -> dict:
        """Devuelve la metadata billing respuesta."""
        plan = build_billing_response_plan(
            message=message,
            contract=contract,
            conversation_state=conversation_state,
            reply_goal=reply_goal,
            hypothesis=hypothesis,
            next_step=next_step,
            followup_prompt=followup_prompt,
            should_handoff=should_handoff,
            handoff_reason=handoff_reason,
            proof_attempts=proof_attempts,
            ocr_status=ocr_status,
            reconnect_status=reconnect_status,
        )
        metadata = {"contract": contract, **dict(extra or {})}
        return response_plan_metadata(plan, metadata)

    async def _billing_handoff(
        self,
        *,
        message: InboundMessage,
        summary: str,
        final: str,
        attachments: list[dict] | None = None,
    ) -> AgentResult:
        """Devuelve la derivacion de facturacion."""
        logger.info(
            "billing_handoff_requested session_id=%s recipient=%s summary=%r",
            message.session_id,
            message.recipient,
            summary[:220],
        )
        try:
            handoff_result = await self.handoff.escalate_new_client(
                channel=message.channel,
                recipient=message.recipient,
                summary=summary,
                cedula=message.cedula,
                origen="ia",
            )
            await self.handoff.relay_attachments(
                channel=message.channel,
                recipient=message.recipient,
                attachments=attachments or [item.model_dump() if hasattr(item, "model_dump") else item for item in (message.attachments or [])],
                cedula=message.cedula,
                origen="ia",
                internal_user=handoff_result.get("internal_user") if isinstance(handoff_result, dict) else None,
                channel_id=handoff_result.get("channel_id") if isinstance(handoff_result, dict) else None,
            )
        except CONTACT_FLOW_EXTERNAL_ERRORS:
            logger.exception("billing_handoff_failed session_id=%s", message.session_id)
            failed_message = (
                "Quise dejar su caso con un asesor especializado para revisar el pago, pero justo ahora no pude completar esa derivación. "
                "Si lo prefiere, vuelva a escribirme en un momento y lo intento otra vez."
            )
            metadata = self._billing_response_metadata(
                contract={},
                message=failed_message,
                conversation_state="handoff_failed",
                reply_goal="ser honesto cuando la derivación de facturación no pudo completarse",
                hypothesis="billing_handoff_requested_but_failed",
                next_step="retry_handoff_later",
                should_handoff=False,
                extra={"summary": summary, "handoff_failed": True, "handoff_group": "support", "handoff_origen": "ia"},
            )
            return AgentResult(
                message=failed_message,
                intent="billing_handoff_failed",
                agent="billing",
                metadata=metadata,
            )
        metadata = self._billing_response_metadata(
            contract={},
            message=final,
            conversation_state="handoff",
            reply_goal="escalar la revisión de pago con honestidad",
            should_handoff=True,
            handoff_reason=summary,
            extra={"summary": summary, "handoff_group": "support", "handoff_origen": "ia"},
        )
        return AgentResult(
            message=final,
            intent="billing_handoff",
            agent="handoff",
            metadata=metadata,
        )

    async def _handle_billing(
        self,
        *,
        message: InboundMessage,
        state: SessionState,
        contract: dict,
        just_selected_contract: bool = False,
    ) -> AgentResult:
        """Maneja billing y avanza el flujo."""
        contact_state = self._state(state)
        billing_state = contact_state.setdefault(
            "billing",
            {
                "awaiting_action": False,
                "awaiting_proof": False,
                "proof_attempts": 0,
                "proof_failures": [],
                "processing_async": False,
            },
        )
        turn_decision = await self._interpret_active_contact_turn(
            message=message,
            state=state,
            preferred_domain="billing",
            contract=contract,
            contracts=contact_state.get("contracts") or [],
        )
        turn_slots = turn_decision.slot_updates or {}
        action = "" if just_selected_contract else normalize_billing_action(message.mensaje)
        if not action and turn_slots.get("billing_action") == "register_payment":
            action = "Registrar Pago"
        elif not action and turn_slots.get("billing_action") == "payment_link":
            action = "Link de Cobro"
        logger.info(
            "billing_flow_state session_id=%s contract=%s action=%s awaiting_action=%s awaiting_proof=%s processing_async=%s attachments=%s turn_action=%s",
            message.session_id,
            contract.get("code"),
            action or None,
            billing_state.get("awaiting_action"),
            billing_state.get("awaiting_proof"),
            billing_state.get("processing_async"),
            len(message.attachments or []),
            turn_decision.action,
        )

        if billing_state.get("processing_async"):
            logger.info(
                "billing_processing_async_suppressed session_id=%s contract=%s attachments=%s",
                message.session_id,
                contract.get("code"),
                len(message.attachments or []),
            )
            return AgentResult(
                message="",
                intent="billing_processing_async",
                agent="billing",
                metadata={"contract": contract, "skip_delivery": True},
            )

        if action == "Link de Cobro":
            billing_state["awaiting_action"] = True
            billing_state["awaiting_proof"] = False
            billing_state["processing_async"] = False
            message_text = f"Claro, aquí tiene el enlace de pago: {payment_link(message.cedula or state.cedula)}"
            return AgentResult(
                message=message_text,
                intent="billing_link",
                agent="billing",
                metadata=self._billing_response_metadata(
                    contract=contract,
                    message=message_text,
                    conversation_state="billing_link",
                    reply_goal="entregar el enlace de pago de forma clara y directa",
                    hypothesis="customer_requested_payment_link",
                    next_step="wait_payment_or_new_question",
                ),
            )

        if action == "Registrar Pago" or message.attachments:
            if not message.attachments:
                billing_state["awaiting_action"] = False
                billing_state["awaiting_proof"] = True
                billing_state["processing_async"] = False
                message_text = format_billing_proof_request()
                return AgentResult(
                    message=message_text,
                    intent="billing_proof_requested",
                    agent="billing",
                    metadata=self._billing_response_metadata(
                        contract=contract,
                        message=message_text,
                        conversation_state="proof_requested",
                        reply_goal="pedir el comprobante con una instrucción simple",
                        hypothesis="awaiting_payment_proof",
                        next_step="await_proof_upload",
                        followup_prompt=(
                            "Envíamelo en imagen o PDF procurando que se vea claro el número del documento, "
                            "la fecha y el monto."
                        ),
                    ),
                )

            billing_state["pending_proof_attachments"] = []
            if self.settings.ocr_async_enabled:
                job = OCRJob(
                    job_id=uuid.uuid4().hex,
                    session_id=message.session_id,
                    channel=message.channel,
                    recipient=message.recipient,
                    cedula=message.cedula or state.cedula,
                    contract=contract,
                    attachments=[item.model_dump() if hasattr(item, "model_dump") else item for item in message.attachments],
                    message=message.mensaje,
                    proof_attempts=int(billing_state.get("proof_attempts") or 0),
                )
                try:
                    queue_result = await self.ocr_queue.enqueue(job)
                except OCRQueueUnavailableError as exc:
                    logger.warning(
                        "billing_proof_queue_unavailable session_id=%s contract=%s error=%s",
                        message.session_id,
                        contract.get("code"),
                        exc,
                    )
                    summary = format_billing_handoff_summary(
                        reason="No fue posible enviar el comprobante a la validación OCR",
                        contract=contract,
                    )
                    self._reset_billing_state(contact_state)
                    return await self._billing_handoff(
                        message=message,
                        summary=summary,
                        final=(
                            "Ya recibí su comprobante, pero en este momento no puedo enviarlo a validación automática. "
                            "Voy a dejar su caso con un asesor especializado para que continúe con su atención."
                        ),
                    )
                logger.info(
                    "billing_proof_queued session_id=%s contract=%s job_id=%s queue_backend=%s queue_size=%s",
                    message.session_id,
                    contract.get("code"),
                    job.job_id,
                    queue_result.get("backend"),
                    queue_result.get("size"),
                )
                billing_state["awaiting_action"] = False
                billing_state["awaiting_proof"] = False
                billing_state["processing_async"] = True
                message_text = (
                    "Listo, ya recibí su comprobante y ya lo envié a validación. "
                    "Le escribiré apenas termine el proceso de revisión."
                )
                return AgentResult(
                    message=message_text,
                    intent="billing_proof_queued",
                    agent="billing",
                    metadata=self._billing_response_metadata(
                        contract=contract,
                        message=message_text,
                        conversation_state="proof_queued",
                        reply_goal="confirmar que el comprobante ya entró a validación",
                        hypothesis="proof_sent_to_async_validation",
                        next_step="wait_async_validation",
                        extra={"ocr_job_id": job.job_id, "queue": queue_result},
                    ),
                )

            if self.ocr.enabled:
                try:
                    ocr_result = await self.ocr.analyze(message.attachments[0], notify_gonet_bot=False)
                except CONTACT_FLOW_EXTERNAL_ERRORS:
                    logger.exception("billing_ocr_failed session_id=%s", message.session_id)
                    ocr_result = None
                if ocr_result:
                    logger.info(
                        "billing_ocr_result session_id=%s contract=%s status=%s retry=%s",
                        message.session_id,
                        contract.get("code"),
                        ocr_result.get("status") or ocr_result.get("estado"),
                        ocr_result.get("debe_reintentar"),
                    )
                    billing_state["awaiting_action"] = False
                    billing_state["awaiting_proof"] = bool(ocr_result.get("debe_reintentar"))
                    if ocr_result.get("debe_reintentar") and not self.billing.can_override_retry_from_ocr(
                        contract=contract,
                        ocr_result=ocr_result,
                    ):
                        attempts = int(billing_state.get("proof_attempts") or 0) + 1
                        proof_failures = list(billing_state.get("proof_failures") or [])
                        proof_failures.append(describe_billing_proof_failure(ocr_result, attempts))
                        billing_state["proof_attempts"] = attempts
                        billing_state["proof_failures"] = proof_failures
                        if attempts >= 2:
                            summary = format_billing_handoff_summary(
                                reason="No se pudo validar el comprobante tras 2 intentos",
                                contract=contract,
                                ocr_result=ocr_result,
                                proof_attempts=attempts,
                                proof_failures=proof_failures,
                            )
                            self._reset_billing_state(contact_state)
                            return await self._billing_handoff(
                                message=message,
                                summary=summary,
                                final=(
                                    "No pude validar bien el comprobante después de 2 intentos. "
                                    "Voy a dejarlo en revisión para que un asesor especializado continúe con su caso."
                                ),
                            )
                        message_text = format_billing_async_retry_message(kind="retry_image", attempt=attempts)
                        return AgentResult(
                            message=message_text,
                            intent="billing_proof_retry",
                            agent="billing",
                            metadata=self._billing_response_metadata(
                                contract=contract,
                                message=message_text,
                                conversation_state="proof_retry",
                                reply_goal="pedir un nuevo comprobante porque el actual no se leyó bien",
                                hypothesis="proof_image_low_quality",
                                next_step="await_clearer_proof",
                                followup_prompt=(
                                    "Reenvíamelo más claro o como archivo completo, procurando que se vea el número "
                                    "del documento, la fecha y el monto."
                                ),
                                proof_attempts=attempts,
                                ocr_status=str(ocr_result.get("status") or ocr_result.get("estado") or ""),
                                extra={"ocr": ocr_result},
                            ),
                        )
                    if ocr_result.get("debe_reintentar"):
                        billing_state["awaiting_proof"] = False
                        logger.info(
                            "billing_ocr_retry_overridden session_id=%s contract=%s reasons=%s",
                            message.session_id,
                            contract.get("code"),
                            ocr_result.get("motivos_reintento") or ocr_result.get("retry_reasons") or [],
                        )
                    try:
                        registration = await self.billing.register_payment(
                            contract=contract,
                            ocr_result=ocr_result,
                            attachments=[item.model_dump() if hasattr(item, "model_dump") else item for item in message.attachments],
                            cedula=message.cedula or state.cedula,
                        )
                    except CONTACT_FLOW_EXTERNAL_ERRORS:
                        logger.exception("billing_register_failed session_id=%s", message.session_id)
                        registration = {"status": "error", "resolved": {}}
                    logger.info(
                        "billing_registration_result session_id=%s contract=%s status=%s reconnect_status=%s",
                        message.session_id,
                        contract.get("code"),
                        registration.get("status"),
                        ((registration.get("resolved") or {}).get("reconnect_status")),
                    )

                    if registration.get("status") == "duplicate":
                        attempts = int(billing_state.get("proof_attempts") or 0) + 1
                        proof_failures = list(billing_state.get("proof_failures") or [])
                        proof_failures.append(describe_billing_proof_failure({"status": "duplicate"}, attempts))
                        billing_state["proof_attempts"] = attempts
                        billing_state["proof_failures"] = proof_failures
                        if attempts >= 2:
                            summary = format_billing_handoff_summary(
                                reason="El comprobante ya estaba registrado",
                                contract=contract,
                                ocr_result=ocr_result,
                                proof_attempts=attempts,
                                proof_failures=proof_failures,
                            )
                            self._reset_billing_state(contact_state)
                            return await self._billing_handoff(
                                message=message,
                                summary=summary,
                                final=format_billing_duplicate_message(escalate=True),
                                attachments=[item.model_dump() if hasattr(item, "model_dump") else item for item in (message.attachments or [])],
                            )
                        billing_state["awaiting_action"] = False
                        billing_state["awaiting_proof"] = True
                        message_text = format_billing_duplicate_message()
                        return AgentResult(
                            message=message_text,
                            intent="billing_payment_duplicate",
                            agent="billing",
                            metadata=self._billing_response_metadata(
                                contract=contract,
                                message=message_text,
                                conversation_state="payment_duplicate",
                                reply_goal="explicar con claridad que el pago ya estaba registrado",
                                hypothesis="payment_already_registered",
                                next_step="wait_new_proof_or_handoff",
                                proof_attempts=attempts,
                                ocr_status=str(ocr_result.get("status") or ocr_result.get("estado") or ""),
                                extra={"ocr": ocr_result, "registration": registration, "proof_attempts": attempts, "proof_failures": proof_failures},
                            ),
                        )

                    if registration.get("status") == "created":
                        self._reset_billing_state(contact_state)
                        reconnect_status = str(((registration.get("resolved") or {}).get("reconnect_status") or "")).strip()
                        if reconnect_status == "pending_balance":
                            balance_due = ((registration.get("resolved") or {}).get("balance_due") or 0.0)
                            message_text = (
                                "Listo, pude validar el comprobante y registrar la gestión de pago, "
                                f"pero aún tiene un saldo pendiente de ${balance_due:.2f}. "
                                "Voy a dejar su caso con un asesor especializado para continuar con la revisión."
                            )
                            summary = format_billing_handoff_summary(
                                reason="El comprobante dejó un saldo pendiente",
                                contract=contract,
                                registration=registration,
                                ocr_result=ocr_result,
                            )
                            return await self._billing_handoff(
                                message=message,
                                summary=summary,
                                final=message_text,
                                attachments=[item.model_dump() if hasattr(item, "model_dump") else item for item in (message.attachments or [])],
                            )
                        if reconnect_status == "done":
                            message_text = (
                                "Perfecto, pude validar el comprobante, registrar la gestión de pago "
                                "y dejar el servicio reconectado."
                            )
                            return AgentResult(
                                message=message_text,
                                intent="billing_payment_registered_reconnected",
                                agent="billing",
                                metadata=self._billing_response_metadata(
                                    contract=contract,
                                    message=message_text,
                                    conversation_state="payment_registered_reconnected",
                                    reply_goal="confirmar que el pago quedó gestionado y el servicio reconectado",
                                    hypothesis="payment_registered_and_reconnected",
                                    next_step="wait_new_request",
                                    ocr_status=str(ocr_result.get("status") or ocr_result.get("estado") or ""),
                                    reconnect_status="done",
                                    extra={"ocr": ocr_result, "registration": registration},
                                ),
                            )
                        if reconnect_status == "error":
                            message_text = (
                                "Pude validar el comprobante y registrar la gestión de pago, "
                                "pero no pude completar la reconexión automática en este momento. "
                                "Voy a dejar su caso con un asesor especializado para continuar con la revisión."
                            )
                            return AgentResult(
                                message=message_text,
                                intent="billing_payment_registered",
                                agent="billing",
                                metadata=self._billing_response_metadata(
                                    contract=contract,
                                    message=message_text,
                                    conversation_state="payment_registered_reconnect_error",
                                    reply_goal="confirmar el pago y avisar que la reconexión automática falló",
                                    hypothesis="payment_registered_reconnect_failed",
                                    next_step="wait_final_validation",
                                    ocr_status=str(ocr_result.get("status") or ocr_result.get("estado") or ""),
                                    reconnect_status="error",
                                    extra={"ocr": ocr_result, "registration": registration},
                                ),
                            )
                        message_text = (
                            "Perfecto, pude validar el comprobante y registrar la gestión de pago. "
                            "La validación final queda en seguimiento con un asesor especializado."
                        )
                        return AgentResult(
                            message=message_text,
                            intent="billing_payment_registered",
                            agent="billing",
                            metadata=self._billing_response_metadata(
                                contract=contract,
                                message=message_text,
                                conversation_state="payment_registered",
                                reply_goal="confirmar que el pago quedó registrado correctamente",
                                hypothesis="payment_registered",
                                next_step="wait_final_validation",
                                ocr_status=str(ocr_result.get("status") or ocr_result.get("estado") or ""),
                                reconnect_status=reconnect_status or "none",
                                extra={"ocr": ocr_result, "registration": registration},
                            ),
                        )

                    partial_balance = self.billing.partial_balance_followup(registration)
                    if partial_balance:
                        self._reset_billing_state(contact_state)
                        message_text = (
                            "Pude validar el comprobante. Registré un pago de "
                            f"${partial_balance['paid_value']:.2f}, pero su deuda actual es de "
                            f"${partial_balance['pending_value']:.2f}, así que todavía le faltan "
                            f"${partial_balance['balance_due']:.2f} para completar el valor pendiente. "
                            "Voy a dejar su caso con un asesor especializado para continuar con la revisión."
                        )
                        summary = format_billing_handoff_summary(
                            reason="El comprobante cubre solo una parte del valor pendiente",
                            contract=contract,
                            registration=registration,
                            ocr_result=ocr_result,
                        )
                        return await self._billing_handoff(
                            message=message,
                            summary=summary,
                            final=message_text,
                            attachments=[item.model_dump() if hasattr(item, "model_dump") else item for item in (message.attachments or [])],
                        )

                    summary = format_billing_handoff_summary(
                        reason="No se pudo registrar el pago automáticamente",
                        contract=contract,
                        registration=registration,
                        ocr_result=ocr_result,
                    )
                    self._reset_billing_state(contact_state)
                    if registration.get("status") == "date_out_of_range":
                        return await self._billing_handoff(
                            message=message,
                            summary=summary,
                            final=(
                                "No fue posible procesar el pago automáticamente porque la fecha del comprobante "
                                "supera los 15 días permitidos. Voy a dejar su caso con un asesor especializado."
                            ),
                        )
                    return await self._billing_handoff(
                        message=message,
                        summary=summary,
                        final=(
                            "Pude validar el comprobante, pero no pude registrar el pago automáticamente. "
                            "Voy a dejar su caso con un asesor especializado."
                        ),
                    )

            billing_state["awaiting_action"] = False
            billing_state["awaiting_proof"] = True
            message_text = "Ya recibí el comprobante, pero en esta etapa todavía dependo del OCR legacy para validarlo por completo."
            return AgentResult(
                message=message_text,
                intent="billing_proof_pending",
                agent="billing",
                metadata=self._billing_response_metadata(
                    contract=contract,
                    message=message_text,
                    conversation_state="proof_pending_legacy",
                    reply_goal="dejar claro que el comprobante quedó pendiente por una limitación operativa",
                    hypothesis="legacy_ocr_pending",
                    next_step="wait_or_retry_later",
                ),
            )

        if billing_state.get("awaiting_proof"):
            billing_state["awaiting_action"] = False
            billing_state["awaiting_proof"] = True
            billing_state["processing_async"] = False
            message_text = format_billing_proof_nudge()
            return AgentResult(
                message=message_text,
                intent="billing_proof_pending",
                agent="billing",
                metadata=self._billing_response_metadata(
                    contract=contract,
                    message=message_text,
                conversation_state="awaiting_proof",
                reply_goal="recordar con naturalidad que falta el comprobante",
                hypothesis="proof_not_received_yet",
                next_step="await_proof_upload",
                followup_prompt=(
                    "Envíemelo en imagen o PDF cuando lo desee, procurando que se vea claro el número del documento, "
                    "la fecha y el monto."
                ),
            ),
        )

        if billing_state.get("awaiting_action"):
            billing_state["awaiting_action"] = True
            billing_state["awaiting_proof"] = False
            billing_state["processing_async"] = False
            message_text = format_billing_action_nudge()
            return AgentResult(
                message=message_text,
                intent="billing_action_clarify",
                agent="billing",
                metadata=self._billing_response_metadata(
                    contract=contract,
                    message=message_text,
                    conversation_state="awaiting_action",
                    reply_goal="pedir una acción concreta sin sonar a menú",
                    hypothesis="needs_billing_action",
                    next_step="await_billing_action_choice",
                ),
            )

        billing_state["awaiting_action"] = True
        billing_state["awaiting_proof"] = False
        billing_state["processing_async"] = False
        message_text = format_billing_options(contract, message.cedula or state.cedula)
        return AgentResult(
            message=message_text,
            intent="billing",
            agent="billing",
            metadata=self._billing_response_metadata(
                contract=contract,
                message=message_text,
                conversation_state="billing_options",
                reply_goal="explicar el estado de facturación y ofrecer la siguiente acción útil",
                hypothesis="customer_needs_billing_help",
                next_step="await_billing_action_choice",
            ),
        )
