"""Procesamiento asíncrono de resultados OCR para facturacion."""

import logging
from datetime import datetime, timezone

from packages.agents.contact_utils import (
    classify_billing_proof_error_kind,
    describe_billing_proof_failure,
    format_billing_async_retry_message,
    format_billing_duplicate_message,
    format_billing_handoff_summary,
)
from packages.channels.delivery import ChannelDeliveryService
from packages.integrations.billing_registration import BillingRegistrationService
from packages.integrations.contact_registry import build_contact_registry
from packages.integrations.ocr_service_client import OCRServiceClient
from packages.integrations.odoo_chat import OdooChatClient
from packages.integrations.redis_store import build_session_store
from packages.orchestrator.response_composer import ResponseComposer
from packages.shared.response_planner import build_billing_response_plan, response_plan_metadata
from packages.shared.config import get_settings
from packages.shared.schemas import Attachment, OCRJob, SessionState

logger = logging.getLogger("billing_async")


class BillingAsyncProcessor:
    """Procesador asíncrono de los resultados OCR de facturación."""

    def __init__(self) -> None:
        """Inicializa el procesador asincrono de los resultados ocr de facturacion con la configuracion necesaria."""
        self.settings = get_settings()
        self.store = build_session_store()
        self.ocr = OCRServiceClient()
        self.billing = BillingRegistrationService()
        self.handoff = OdooChatClient()
        self.delivery = ChannelDeliveryService()
        self.contacts = build_contact_registry()
        self.response_composer = ResponseComposer()

    async def _compose_direct_message(
        self,
        *,
        state: SessionState,
        job: OCRJob,
        message: str,
        intent: str = "billing_async_result",
        agent: str = "billing",
        reason: str = "billing_async_direct",
        metadata: dict | None = None,
    ) -> str:
        """Compone mensaje de direct respetando el contexto."""
        try:
            rendered = await self.response_composer.compose_direct_result(
                state=state,
                channel=job.channel,
                recipient=job.recipient,
                raw_message=message,
                intent=intent,
                result_agent=agent,
                decision_agent=agent,
                reason=reason,
                user_message=job.message,
                metadata={
                    "contract": dict(job.contract or {}),
                    "billing_async": True,
                    **dict(metadata or {}),
                },
            )
            return str(rendered.message or message).strip() or str(message or "")
        except Exception:
            logger.exception("billing_async_compose_failed session_id=%s", job.session_id)
            return str(message or "")

    @staticmethod
    def _contact_state(state: SessionState) -> dict:
        """Devuelve el estado de contacto."""
        contact = state.metadata.get("contact") or {}
        contact.setdefault(
            "billing",
            {
                "awaiting_action": False,
                "awaiting_proof": False,
                "proof_attempts": 0,
                "proof_failures": [],
                "processing_async": False,
            },
        )
        state.metadata["contact"] = contact
        return contact

    async def _load_state(self, job: OCRJob) -> SessionState:
        """Carga estado."""
        state = await self.store.get(job.session_id)
        if state is None:
            state = SessionState(
                session_id=job.session_id,
                channel=job.channel,
                recipient=job.recipient,
                cedula=job.cedula,
            )
        return state

    @staticmethod
    def _reset_billing_state(contact_state: dict) -> None:
        """Reinicia billing state para comenzar de nuevo."""
        billing = contact_state.setdefault("billing", {})
        billing["awaiting_action"] = False
        billing["awaiting_proof"] = False
        billing["proof_attempts"] = 0
        billing["proof_failures"] = []
        billing["processing_async"] = False

    @staticmethod
    def _billing_metadata(
        *,
        job: OCRJob,
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
        """Devuelve la metadata billing."""
        plan = build_billing_response_plan(
            message=message,
            contract=job.contract,
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
        return response_plan_metadata(plan, dict(extra or {}))

    async def _save_and_send(
        self,
        *,
        state: SessionState,
        job: OCRJob,
        message: str,
        source: str,
        registry_mode: str = "ia",
        intent: str = "billing_async_result",
        agent: str = "billing",
        metadata: dict | None = None,
    ) -> None:
        """Guarda and send."""
        message = await self._compose_direct_message(
            state=state,
            job=job,
            message=message,
            intent=intent,
            agent=agent,
            reason=source,
            metadata=metadata,
        )
        now = datetime.now(timezone.utc)
        state.history.append({"role": "assistant", "content": message})
        state.history = state.history[-20:]
        state.updated_at = now
        state.last_assistant_message_at = now
        await self.store.set(state)
        if registry_mode == "human":
            await self.contacts.mark_human_active(
                recipient=job.recipient,
                red=job.channel,
                identificacion=state.cedula or job.cedula,
                session_id=job.session_id,
                group="support",
                chat_preview=message,
            )
        else:
            await self.contacts.mark_ai_active(
                recipient=job.recipient,
                red=job.channel,
                identificacion=state.cedula or job.cedula,
                session_id=job.session_id,
                group="support",
                chat_preview=message,
            )
        logger.info(
            "billing_async_send session_id=%s recipient=%s source=%s preview=%r",
            job.session_id,
            job.recipient,
            source,
            " ".join((message or "").split())[:160],
        )
        await self.delivery.deliver(
            channel=job.channel,
            recipient=job.recipient,
            message=message,
        )

    async def _handoff(self, *, state: SessionState, job: OCRJob, summary: str, final_message: str, source: str) -> bool:
        """Devuelve el handoff."""
        logger.info(
            "billing_async_handoff session_id=%s recipient=%s source=%s summary=%r",
            job.session_id,
            job.recipient,
            source,
            summary[:220],
        )
        try:
            handoff_result = await self.handoff.escalate_new_client(
                channel=job.channel,
                recipient=job.recipient,
                summary=summary,
                cedula=job.cedula,
                origen="ia",
            )
        except Exception:
            logger.exception("billing_async_handoff_failed session_id=%s", job.session_id)
            await self._save_and_send(
                state=state,
                job=job,
                message=(
                    "Su comprobante necesita revisión manual, pero ahora mismo no pude dejarlo con un asesor especializado. "
                    "Por favor inténtelo nuevamente en unos minutos."
                ),
                source=source,
                metadata=self._billing_metadata(
                    job=job,
                    message=(
                        "Su comprobante necesita revisión manual, pero ahora mismo no pude dejarlo con un asesor especializado. "
                        "Por favor inténtelo nuevamente en unos minutos."
                    ),
                    conversation_state="handoff_failed",
                    reply_goal="ser honesto sobre la falla operativa y no prometer un handoff inexistente",
                    hypothesis="manual_review_needed_but_handoff_failed",
                    next_step="retry_later",
                    should_handoff=False,
                    extra={"handoff_failed": True},
                ),
            )
            return False
        try:
            await self.handoff.relay_attachments(
                channel=job.channel,
                recipient=job.recipient,
                attachments=job.attachments,
                cedula=job.cedula,
                origen="ia",
                internal_user=handoff_result.get("internal_user") if isinstance(handoff_result, dict) else None,
                channel_id=handoff_result.get("channel_id") if isinstance(handoff_result, dict) else None,
            )
        except Exception:
            logger.exception("billing_async_handoff_attachment_relay_failed session_id=%s", job.session_id)
        state.human_handoff = True
        state.current_intent = "human_handoff"
        state.last_agent = "handoff"
        state.metadata["handoff_group"] = "support"
        state.metadata["handoff_origen"] = "ia"
        await self._save_and_send(
            state=state,
            job=job,
            message=final_message,
            source=source,
            registry_mode="human",
                metadata=self._billing_metadata(
                    job=job,
                    message=final_message,
                    conversation_state="handoff_created",
                    reply_goal="confirmar que el caso ya quedó derivado a un asesor especializado",
                    hypothesis="manual_review_needed",
                    next_step="wait_human_followup",
                    should_handoff=True,
                handoff_reason=summary,
                extra={"handoff_created": True},
            ),
        )
        return True

    async def process_result(self, job: OCRJob, ocr_result: dict | None, *, source: str = "ocr_service") -> dict:
        """Procesa la entrada y avanza el flujo."""
        logger.info(
            "billing_async_process_result_start session_id=%s job_id=%s source=%s contract=%s ocr_status=%s retry=%s",
            job.session_id,
            job.job_id,
            source,
            (job.contract or {}).get("code"),
            None if not ocr_result else (ocr_result.get("status") or ocr_result.get("estado")),
            None if not ocr_result else ocr_result.get("debe_reintentar"),
        )
        state = await self._load_state(job)
        contact_state = self._contact_state(state)
        billing_state = contact_state.setdefault("billing", {})
        billing_state["processing_async"] = False

        if not ocr_result or ocr_result.get("status") in {"unavailable", "error"}:
            attempts = int(billing_state.get("proof_attempts") or job.proof_attempts or 0) + 1
            failures = list(billing_state.get("proof_failures") or [])
            failure_result = ocr_result or {"status": "error"}
            failures.append(describe_billing_proof_failure(failure_result, attempts))
            billing_state["proof_attempts"] = attempts
            billing_state["proof_failures"] = failures
            failure_kind = classify_billing_proof_error_kind(ocr_result)
            if attempts < 2 and failure_kind in {"temporary", "retry_image"}:
                billing_state["awaiting_action"] = False
                billing_state["awaiting_proof"] = True
                retry_message = format_billing_async_retry_message(kind=failure_kind, attempt=attempts)
                await self._save_and_send(
                    state=state,
                    job=job,
                    message=retry_message,
                    source=source,
                    metadata=self._billing_metadata(
                        job=job,
                        message=retry_message,
                        conversation_state="async_retry",
                        reply_goal="pedir un nuevo comprobante antes de escalar a un asesor especializado",
                        hypothesis=f"ocr_{failure_kind}",
                        next_step="await_proof_upload",
                        followup_prompt="Reenvíemelo una vez más y lo vuelvo a intentar.",
                        proof_attempts=attempts,
                        ocr_status=str(failure_result.get("status") or failure_result.get("estado") or ""),
                        extra={
                            "billing_async_retry": True,
                            "proof_attempts": attempts,
                            "proof_failures": failures,
                        },
                    ),
                )
                return {"status": "retry", "reason": f"ocr_{failure_kind}", "attempts": attempts}
            summary = format_billing_handoff_summary(
                reason="No fue posible validar automáticamente el comprobante",
                contract=job.contract,
                ocr_result=failure_result,
                proof_attempts=attempts,
                proof_failures=failures,
            )
            self._reset_billing_state(contact_state)
            final_message = (
                "No pude validar el comprobante automáticamente en este momento. Le pondré en contacto con un asesor especializado para revisarlo."
            )
            if failure_kind == "temporary":
                final_message = (
                    "No pude validar el comprobante automáticamente después de intentarlo otra vez. "
                    "Voy a derivar su caso con un asesor especializado para que lo revise."
                )
            handoff_ok = await self._handoff(
                state=state,
                job=job,
                summary=summary,
                final_message=final_message,
                source=source,
            )
            return {"status": "handoff" if handoff_ok else "handoff_failed", "reason": "ocr_error"}

        if ocr_result.get("debe_reintentar") and not self.billing.can_override_retry_from_ocr(
            contract=job.contract,
            ocr_result=ocr_result,
        ):
            attempts = int(billing_state.get("proof_attempts") or 0) + 1
            failures = list(billing_state.get("proof_failures") or [])
            failures.append(describe_billing_proof_failure(ocr_result, attempts))
            billing_state["proof_attempts"] = attempts
            billing_state["proof_failures"] = failures
            billing_state["awaiting_proof"] = attempts < 2
            if attempts >= 2:
                summary = format_billing_handoff_summary(
                    reason="No se pudo validar el comprobante tras 2 intentos",
                    contract=job.contract,
                    ocr_result=ocr_result,
                    proof_attempts=attempts,
                    proof_failures=failures,
                )
                self._reset_billing_state(contact_state)
                handoff_ok = await self._handoff(
                    state=state,
                    job=job,
                    summary=summary,
                    final_message=(
                        "No pude validar bien el comprobante después de 2 intentos. "
                        "Voy a enviarlo a revisión para que un asesor especializado continúe con su caso."
                    ),
                    source=source,
                )
                return {"status": "handoff" if handoff_ok else "handoff_failed", "reason": "ocr_retry_limit"}
            retry_message = "No pude validar bien el comprobante. Envíeme una foto más clara o el archivo completo nuevamente, por favor."
            await self._save_and_send(
                state=state,
                job=job,
                message=retry_message,
                source=source,
                metadata=self._billing_metadata(
                    job=job,
                    message=retry_message,
                    conversation_state="async_retry",
                    reply_goal="pedir otra imagen porque el comprobante no se leyó con suficiente claridad",
                    hypothesis="ocr_retry_image",
                    next_step="await_clearer_proof",
                    followup_prompt="Reenvíemelo más claro o como archivo completo, por favor.",
                    proof_attempts=attempts,
                    ocr_status=str(ocr_result.get("status") or ocr_result.get("estado") or ""),
                ),
            )
            return {"status": "retry", "attempts": attempts}
        if ocr_result.get("debe_reintentar"):
            logger.info(
                "billing_async_retry_overridden session_id=%s job_id=%s contract=%s reasons=%s",
                job.session_id,
                job.job_id,
                (job.contract or {}).get("code"),
                ocr_result.get("motivos_reintento") or ocr_result.get("retry_reasons") or [],
            )

        try:
            registration = await self.billing.register_payment(
                contract=job.contract,
                ocr_result=ocr_result,
                attachments=job.attachments,
                cedula=job.cedula,
            )
        except Exception:
            logger.exception("billing_async_register_failed session_id=%s", job.session_id)
            registration = {"status": "error", "resolved": {}}
        logger.info(
            "billing_async_registration_result session_id=%s job_id=%s status=%s reconnect_status=%s",
            job.session_id,
            job.job_id,
            registration.get("status"),
            ((registration.get("resolved") or {}).get("reconnect_status")),
        )

        if registration.get("status") == "duplicate":
            attempts = int(billing_state.get("proof_attempts") or 0) + 1
            failures = list(billing_state.get("proof_failures") or [])
            failures.append(describe_billing_proof_failure({"status": "duplicate"}, attempts))
            billing_state["proof_attempts"] = attempts
            billing_state["proof_failures"] = failures
            if attempts >= 2:
                summary = format_billing_handoff_summary(
                    reason="El comprobante ya estaba registrado",
                    contract=job.contract,
                    ocr_result=ocr_result,
                    proof_attempts=attempts,
                    proof_failures=failures,
                )
                self._reset_billing_state(contact_state)
                handoff_ok = await self._handoff(
                    state=state,
                    job=job,
                    summary=summary,
                    final_message=format_billing_duplicate_message(escalate=True),
                    source=source,
                )
                return {"status": "handoff" if handoff_ok else "handoff_failed", "reason": "duplicate_payment"}
            billing_state["awaiting_action"] = False
            billing_state["awaiting_proof"] = True
            duplicate_message = format_billing_duplicate_message()
            await self._save_and_send(
                state=state,
                job=job,
                message=duplicate_message,
                source=source,
                metadata=self._billing_metadata(
                    job=job,
                    message=duplicate_message,
                    conversation_state="async_duplicate",
                    reply_goal="explicar que el comprobante ya estaba registrado",
                    hypothesis="payment_already_registered",
                    next_step="wait_new_proof_or_handoff",
                    proof_attempts=attempts,
                    ocr_status=str(ocr_result.get("status") or ocr_result.get("estado") or ""),
                    extra={
                        "billing_async_duplicate": True,
                        "proof_attempts": attempts,
                        "proof_failures": failures,
                    },
                ),
            )
            return {"status": "duplicate", "attempts": attempts}

        if registration.get("status") == "created":
            self._reset_billing_state(contact_state)
            reconnect_status = str(((registration.get("resolved") or {}).get("reconnect_status") or "")).strip()
            if reconnect_status == "pending_balance":
                balance_due = ((registration.get("resolved") or {}).get("balance_due") or 0.0)
                pending_balance_message = (
                    "Pude validar el comprobante y registrar la gestión de pago, "
                    f"pero aún tiene un saldo pendiente de ${balance_due:.2f}. "
                    "Voy a dejar su caso con un asesor especializado para continuar con la revisión."
                )
                handoff_ok = await self._handoff(
                    state=state,
                    job=job,
                    summary=format_billing_handoff_summary(
                        reason="El comprobante dejó un saldo pendiente",
                        contract=job.contract,
                        registration=registration,
                        ocr_result=ocr_result,
                    ),
                    final_message=pending_balance_message,
                    source=source,
                )
                return {"status": "handoff" if handoff_ok else "handoff_failed", "reconnect_status": "pending_balance"}
            if reconnect_status == "done":
                reconnected_message = (
                    "Perfecto, pude validar el comprobante, registrar la gestión de pago "
                    "y dejar el servicio reconectado."
                )
                await self._save_and_send(
                    state=state,
                    job=job,
                    message=reconnected_message,
                    source=source,
                    metadata=self._billing_metadata(
                        job=job,
                        message=reconnected_message,
                        conversation_state="async_registered_reconnected",
                        reply_goal="confirmar que el pago quedó gestionado y el servicio ya fue reconectado",
                        hypothesis="payment_registered_and_reconnected",
                        next_step="wait_new_request",
                        ocr_status=str(ocr_result.get("status") or ocr_result.get("estado") or ""),
                        reconnect_status="done",
                    ),
                )
                return {"status": "created", "reconnect_status": "done"}
            if reconnect_status == "error":
                reconnect_error_message = (
                    "Pude validar el comprobante y registrar la gestión de pago, "
                    "pero no pude completar la reconexión automática en este momento. "
                    "Voy a dejar su caso con un asesor especializado para continuar con la revisión."
                )
                await self._save_and_send(
                    state=state,
                    job=job,
                    message=reconnect_error_message,
                    source=source,
                    metadata=self._billing_metadata(
                        job=job,
                        message=reconnect_error_message,
                        conversation_state="async_registered_reconnect_error",
                        reply_goal="confirmar el pago y dejar claro que la reconexión automática falló",
                        hypothesis="payment_registered_reconnect_failed",
                        next_step="wait_final_validation",
                        ocr_status=str(ocr_result.get("status") or ocr_result.get("estado") or ""),
                        reconnect_status="error",
                    ),
                )
                return {"status": "created", "reconnect_status": "error"}
            registered_message = (
                "Perfecto, pude validar el comprobante y registrar la gestión de pago. "
                "La validación final queda en seguimiento con un asesor especializado."
            )
            await self._save_and_send(
                state=state,
                job=job,
                message=registered_message,
                source=source,
                metadata=self._billing_metadata(
                    job=job,
                    message=registered_message,
                    conversation_state="async_registered",
                    reply_goal="confirmar que el pago quedó registrado correctamente",
                    hypothesis="payment_registered",
                    next_step="wait_final_validation",
                    ocr_status=str(ocr_result.get("status") or ocr_result.get("estado") or ""),
                    reconnect_status=reconnect_status or "none",
                ),
            )
            return {"status": "created", "reconnect_status": reconnect_status or "none"}

        partial_balance = self.billing.partial_balance_followup(registration)
        if partial_balance:
            self._reset_billing_state(contact_state)
            missing_amount_message = (
                "Pude validar el comprobante. Registré un pago de "
                f"${partial_balance['paid_value']:.2f}, pero su deuda actual es de "
                f"${partial_balance['pending_value']:.2f}, así que todavía le faltan "
                f"${partial_balance['balance_due']:.2f} para completar el valor pendiente. "
                "Voy a dejar su caso con un asesor especializado para continuar con la revisión."
            )
            handoff_ok = await self._handoff(
                state=state,
                job=job,
                summary=format_billing_handoff_summary(
                    reason="El comprobante cubre solo una parte del valor pendiente",
                    contract=job.contract,
                    registration=registration,
                    ocr_result=ocr_result,
                ),
                final_message=missing_amount_message,
                source=source,
            )
            return {"status": "handoff" if handoff_ok else "handoff_failed", "reason": "partial_balance_due"}

        summary = format_billing_handoff_summary(
            reason="No se pudo registrar el pago automáticamente",
            contract=job.contract,
            registration=registration,
            ocr_result=ocr_result,
        )
        self._reset_billing_state(contact_state)
        if registration.get("status") == "date_out_of_range":
            final_message = (
                "No fue posible procesar el pago automáticamente porque la fecha del comprobante "
                "supera los 15 días permitidos. Voy a derivar su caso con un asesor especializado."
            )
        else:
            final_message = (
                "Pude validar el comprobante, pero no pude registrar el pago automáticamente. "
                "Voy a derivar su caso con un asesor especializado."
            )
        handoff_ok = await self._handoff(
            state=state,
            job=job,
            summary=summary,
            final_message=final_message,
            source=source,
        )
        return {"status": "handoff" if handoff_ok else "handoff_failed", "reason": "register_failed"}

    async def process(self, job: OCRJob) -> dict:
        """Procesa la entrada y avanza el flujo."""
        logger.info(
            "billing_async_process_start session_id=%s job_id=%s contract=%s attachments=%s ocr_enabled=%s",
            job.session_id,
            job.job_id,
            (job.contract or {}).get("code"),
            len(job.attachments or []),
            self.ocr.enabled,
        )
        if not self.ocr.enabled:
            return await self.process_result(job, {"status": "unavailable"}, source="worker_ocr")

        try:
            attachment = job.attachments[0]
            if isinstance(attachment, dict):
                attachment = Attachment.model_validate(attachment)
            ocr_result = await self.ocr.analyze(attachment, notify_gonet_bot=False)
        except Exception:
            logger.exception("billing_async_ocr_failed session_id=%s", job.session_id)
            ocr_result = None
        return await self.process_result(job, ocr_result, source="worker_ocr")
