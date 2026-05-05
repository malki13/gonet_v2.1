import asyncio

from packages.agents.billing_async import BillingAsyncProcessor
from packages.channels.delivery import ChannelDeliveryService
from packages.integrations.billing_registration import BillingRegistrationService
from packages.integrations.odoo_chat import OdooChatClient
from packages.integrations.redis_store import build_session_store
from packages.shared.schemas import AgentResult, OCRJob, SessionState


def test_billing_async_handoff_failure_does_not_mark_session_human(monkeypatch):
    sent_messages = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        sent_messages.append(
            {
                "channel": channel,
                "recipient": recipient,
                "message": message,
                "media_type": media_type,
            }
        )
        return {"status": "sent"}

    async def failing_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        raise RuntimeError("odoo down")

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", failing_handoff)

    store = build_session_store()
    asyncio.run(
        store.set(
            SessionState(
                session_id="billing-human-fail-1",
                channel="whatsapp",
                recipient="593998",
                cedula="0102030405",
                current_intent="billing_proof_queued",
                last_agent="billing",
            )
        )
    )

    processor = BillingAsyncProcessor()

    async def fake_compose_direct_result(**kwargs):
        assert kwargs["intent"] == "billing_async_result"
        return AgentResult(message="Mensaje async renderizado", intent=kwargs["intent"], agent=kwargs["result_agent"])

    monkeypatch.setattr(processor.response_composer, "compose_direct_result", fake_compose_direct_result)
    result = asyncio.run(
        processor.process_result(
            OCRJob(
                job_id="job-human-fail-1",
                session_id="billing-human-fail-1",
                channel="whatsapp",
                recipient="593998",
                cedula="0102030405",
                contract={"code": "203493", "state": "cortado", "residual": "22.89"},
                attachments=[],
                message="Comprobante enviado",
            ),
            {"status": "error", "message": "bank mismatch unrecoverable"},
            source="ocr_service_queue",
        )
    )

    assert result["status"] == "handoff_failed"
    state = asyncio.run(store.get("billing-human-fail-1"))
    assert state is not None
    assert state.human_handoff is False
    assert state.current_intent == "billing_proof_queued"
    assert len(sent_messages) == 1
    assert sent_messages[0]["message"] == "Mensaje async renderizado"


def test_billing_async_retryable_ocr_error_requests_resend_before_handoff(monkeypatch):
    sent_messages = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        sent_messages.append(message)
        return {"status": "sent"}

    async def should_not_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        raise AssertionError("handoff should not happen on first retryable OCR failure")

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", should_not_handoff)

    store = build_session_store()
    asyncio.run(
        store.set(
            SessionState(
                session_id="billing-ocr-retry-1",
                channel="whatsapp",
                recipient="593998",
                cedula="0102030405",
                current_intent="billing_proof_queued",
                last_agent="billing",
                metadata={
                    "contact": {
                        "billing": {
                            "awaiting_action": False,
                            "awaiting_proof": False,
                            "proof_attempts": 0,
                            "proof_failures": [],
                            "processing_async": True,
                        }
                    }
                },
            )
        )
    )

    processor = BillingAsyncProcessor()
    result = asyncio.run(
        processor.process_result(
            OCRJob(
                job_id="job-retryable-ocr-1",
                session_id="billing-ocr-retry-1",
                channel="whatsapp",
                recipient="593998",
                cedula="0102030405",
                contract={"code": "203493", "state": "cortado", "residual": "22.89"},
                attachments=[],
                message="Comprobante enviado",
            ),
            {"status": "error", "code": "processing_timeout", "message": "OCR processing timed out"},
            source="ocr_service_queue",
        )
    )

    assert result["status"] == "retry"
    state = asyncio.run(store.get("billing-ocr-retry-1"))
    assert state is not None
    assert state.human_handoff is False
    assert state.last_assistant_message_at is not None
    billing_state = ((state.metadata.get("contact") or {}).get("billing") or {})
    assert billing_state.get("awaiting_proof") is True
    assert billing_state.get("processing_async") is False
    assert billing_state.get("proof_attempts") == 1
    assert sent_messages
    lowered = sent_messages[0].lower()
    assert "reenv" in lowered
    assert "asesor especializado" in lowered
    assert "numero del documento" in lowered or "número del documento" in lowered
    assert "fecha" in lowered
    assert "monto" in lowered


def test_billing_async_uses_registerable_ocr_payload_even_when_ocr_requests_retry(monkeypatch):
    sent_messages = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        sent_messages.append(message)
        return {"status": "sent"}

    async def should_not_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        raise AssertionError("handoff should not happen when the payload is registerable")

    async def fake_register(self, *, contract, ocr_result, attachments, cedula):
        assert ocr_result["debe_reintentar"] is True
        assert contract["code"] == "901204"
        return {
            "status": "created",
            "resolved": {
                "reconnect_status": "done",
            },
        }

    async def fake_compose_direct_result(**kwargs):
        return AgentResult(message=kwargs["raw_message"], intent=kwargs["intent"], agent=kwargs["result_agent"])

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", should_not_handoff)
    monkeypatch.setattr(BillingRegistrationService, "register_payment", fake_register)

    store = build_session_store()
    asyncio.run(
        store.set(
            SessionState(
                session_id="billing-ocr-override-1",
                channel="whatsapp",
                recipient="593998",
                cedula="0102030405",
                current_intent="billing_proof_queued",
                last_agent="billing",
                metadata={
                    "contact": {
                        "billing": {
                            "awaiting_action": False,
                            "awaiting_proof": True,
                            "proof_attempts": 0,
                            "proof_failures": [],
                            "processing_async": True,
                        }
                    }
                },
            )
        )
    )

    processor = BillingAsyncProcessor()
    monkeypatch.setattr(
        processor.billing,
        "can_override_retry_from_ocr",
        lambda *, contract, ocr_result: True,
    )
    monkeypatch.setattr(processor.response_composer, "compose_direct_result", fake_compose_direct_result)

    result = asyncio.run(
        processor.process_result(
            OCRJob(
                job_id="job-ocr-override-1",
                session_id="billing-ocr-override-1",
                channel="whatsapp",
                recipient="593998",
                cedula="0102030405",
                contract={"code": "901204", "state": "cortado", "residual": "17.40"},
                attachments=[{"filename": "comprobante.jpg"}],
                message="Comprobante enviado",
            ),
            {
                "status": "ok",
                "estado": "reintentar_foto",
                "debe_reintentar": True,
                "motivos_reintento": ["faltan_campos_criticos"],
                "texto_extraido": "BANCO PICHINCHA N° de comprobante 59318751 El 01 de abril de 2026 $17.40",
            },
            source="ocr_service_queue",
        )
    )

    assert result["status"] == "created"
    assert result["reconnect_status"] == "done"
    assert sent_messages
    assert "validar el comprobante" in sent_messages[0].lower()
    assert "asesor especializado" not in sent_messages[0].lower()


def test_can_override_retry_from_ocr_allows_missing_critical_fields_when_code_date_amount_exist():
    service = BillingRegistrationService()

    assert service.can_override_retry_from_ocr(
        contract={},
        ocr_result={
            "debe_reintentar": True,
            "motivos_reintento": ["faltan_campos_criticos"],
            "texto_extraido": (
                "Pagaste a GONET\n"
                "Total pagado $17,19\n"
                "Fecha de pago 12 abr 2026 - 08h34\n"
                "Nro. de transacción 759185347061\n"
            ),
        },
    ) is True


def test_can_override_retry_from_ocr_rejects_suspicious_retry_reasons():
    service = BillingRegistrationService()

    assert service.can_override_retry_from_ocr(
        contract={},
        ocr_result={
            "debe_reintentar": True,
            "motivos_reintento": ["numero_documento_inconsistente_ocr"],
            "texto_extraido": (
                "Pagaste a GONET\n"
                "Total pagado $17,19\n"
                "Fecha de pago 12 abr 2026 - 08h34\n"
                "Nro. de transacción 759185347061\n"
            ),
        },
    ) is False


def test_partial_balance_followup_only_applies_to_invoice_gap_above_tolerance():
    service = BillingRegistrationService()

    followup = service.partial_balance_followup(
        {
            "status": "missing_fields",
            "missing": ["invoices"],
            "resolved": {
                "value": 16.76,
                "pending_value": 27.65,
                "balance_due": 10.89,
            },
        }
    )

    assert followup == {
        "pending_value": 27.65,
        "paid_value": 16.76,
        "balance_due": 10.89,
    }


def test_partial_balance_followup_ignores_small_difference_within_tolerance():
    service = BillingRegistrationService()

    followup = service.partial_balance_followup(
        {
            "status": "missing_fields",
            "missing": ["invoices"],
            "resolved": {
                "value": 21.76,
                "pending_value": 27.65,
                "balance_due": 4.89,
            },
        }
    )

    assert followup is None


def test_billing_async_reports_missing_amount_before_handoff(monkeypatch):
    sent_messages = []
    handoffs = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        sent_messages.append(message)
        return {"status": "sent"}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(
            {
                "channel": channel,
                "recipient": recipient,
                "summary": summary,
                "cedula": cedula,
                "origen": origen,
                "group": group,
            }
        )
        return {"status": "sent", "channel_id": 77}

    async def fake_register(self, *, contract, ocr_result, attachments, cedula):
        return {
            "status": "missing_fields",
            "missing": ["invoices"],
            "resolved": {
                "value": 16.76,
                "pending_value": 27.65,
                "balance_due": 10.89,
            },
        }

    async def fake_compose_direct_result(**kwargs):
        return AgentResult(message=kwargs["raw_message"], intent=kwargs["intent"], agent=kwargs["result_agent"])

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)
    monkeypatch.setattr(BillingRegistrationService, "register_payment", fake_register)

    store = build_session_store()
    asyncio.run(
        store.set(
            SessionState(
                session_id="billing-partial-balance-1",
                channel="whatsapp",
                recipient="593998",
                cedula="0102030405",
                current_intent="billing_proof_queued",
                last_agent="billing",
                metadata={
                    "contact": {
                        "billing": {
                            "awaiting_action": False,
                            "awaiting_proof": False,
                            "proof_attempts": 0,
                            "proof_failures": [],
                            "processing_async": True,
                        }
                    }
                },
            )
        )
    )

    processor = BillingAsyncProcessor()
    monkeypatch.setattr(processor.response_composer, "compose_direct_result", fake_compose_direct_result)

    result = asyncio.run(
        processor.process_result(
            OCRJob(
                job_id="job-partial-balance-1",
                session_id="billing-partial-balance-1",
                channel="whatsapp",
                recipient="593998",
                cedula="0102030405",
                contract={"code": "120234", "state": "cortado", "residual": "27.65"},
                attachments=[{"filename": "comprobante.jpg"}],
                message="Comprobante enviado",
            ),
            {
                "status": "ok",
                "estado": "ok",
                "debe_reintentar": False,
                "texto_extraido": "BANCO PICHINCHA $16.76 15 abr 2026 comprobante 77200866",
            },
            source="ocr_service_queue",
        )
    )

    assert result["status"] == "handoff"
    assert sent_messages
    assert len(handoffs) == 1
    assert "saldo pendiente" in (handoffs[0]["summary"] or "").lower()
    lowered = sent_messages[0].lower()
    assert "16.76" in sent_messages[0]
    assert "27.65" in sent_messages[0]
    assert "10.89" in sent_messages[0]
    assert "todavia le faltan" in lowered or "todavía le faltan" in lowered
    assert "asesor especializado" in lowered


def test_billing_async_duplicate_proof_handoffs_on_second_attempt(monkeypatch):
    sent_messages = []
    handoffs = []
    relayed = []

    async def fake_deliver(self, *, channel, recipient, message, actions=None, media_type=None):
        sent_messages.append(message)
        return {"status": "sent"}

    async def fake_handoff(self, *, channel, recipient, summary=None, cedula=None, origen=None, group=None):
        handoffs.append(
            {
                "channel": channel,
                "recipient": recipient,
                "summary": summary,
                "cedula": cedula,
                "origen": origen,
                "group": group,
            }
        )
        return {"status": "sent", "internal_user": 15, "channel_id": 44}

    async def fake_relay_attachments(
        self,
        *,
        channel,
        recipient,
        attachments,
        cedula=None,
        origen=None,
        internal_user=None,
        channel_id=None,
    ):
        relayed.append(
            {
                "channel": channel,
                "recipient": recipient,
                "attachments": attachments,
                "cedula": cedula,
                "origen": origen,
                "internal_user": internal_user,
                "channel_id": channel_id,
            }
        )
        return {"status": "sent"}

    async def fake_register(self, *, contract, ocr_result, attachments, cedula):
        return {
            "status": "duplicate",
            "resolved": {
                "code": "130283",
                "value": 18.00,
                "pending_value": 18.00,
                "deposit": {"name": "BANCO PICHINCHA"},
            },
        }

    async def fake_compose_direct_result(**kwargs):
        return AgentResult(message=kwargs["raw_message"], intent=kwargs["intent"], agent=kwargs["result_agent"])

    monkeypatch.setattr(ChannelDeliveryService, "deliver", fake_deliver)
    monkeypatch.setattr(OdooChatClient, "escalate_new_client", fake_handoff)
    monkeypatch.setattr(OdooChatClient, "relay_attachments", fake_relay_attachments)
    monkeypatch.setattr(BillingRegistrationService, "register_payment", fake_register)

    store = build_session_store()
    asyncio.run(
        store.set(
            SessionState(
                session_id="billing-duplicate-2",
                channel="whatsapp",
                recipient="593998",
                cedula="0102030405",
                current_intent="billing_proof_queued",
                last_agent="billing",
                metadata={
                    "contact": {
                        "billing": {
                            "awaiting_action": False,
                            "awaiting_proof": True,
                            "proof_attempts": 1,
                            "proof_failures": ["Intento 1: el pago ya estaba registrado y el comprobante no es válido como nuevo pago"],
                            "processing_async": False,
                        }
                    }
                },
            )
        )
    )

    processor = BillingAsyncProcessor()
    monkeypatch.setattr(processor.response_composer, "compose_direct_result", fake_compose_direct_result)

    result = asyncio.run(
        processor.process_result(
            OCRJob(
                job_id="job-duplicate-2",
                session_id="billing-duplicate-2",
                channel="whatsapp",
                recipient="593998",
                cedula="0102030405",
                contract={"code": "203493", "state": "cortado", "residual": "22.89"},
                attachments=[{"filename": "proof.png", "mime_type": "image/png"}],
                message="Comprobante enviado",
            ),
            {
                "status": "ok",
                "estado": "validated",
                "debe_reintentar": False,
                "texto_extraido": "Comprobante válido",
            },
            source="ocr_service_queue",
        )
    )

    assert result["status"] == "handoff"
    state = asyncio.run(store.get("billing-duplicate-2"))
    assert state is not None
    billing_state = ((state.metadata.get("contact") or {}).get("billing") or {})
    assert state.human_handoff is True
    assert billing_state.get("proof_attempts") == 0
    assert sent_messages
    lowered = sent_messages[0].lower()
    assert "no es válido" in lowered or "no es valido" in lowered
    assert "asesor especializado" in lowered
    assert len(handoffs) == 1
    assert len(relayed) == 1
    assert relayed[0]["attachments"]
