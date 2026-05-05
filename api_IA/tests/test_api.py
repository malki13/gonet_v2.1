import asyncio
import base64
import io
import os
import threading
import time
from types import SimpleNamespace

import pytest
from werkzeug.datastructures import FileStorage

os.environ.setdefault("OPENAI_API_KEY", "test-key")
from src.ocr_service.api import app_factory
from src.ocr_service.domain.models import APIError, QualityMetrics, UploadedDocument, Usage
from src.ocr_service.services import ocr_core
from src.ocr_service.services.runtime import BoundedExecutor


def _dummy_document(is_pdf: bool = False) -> UploadedDocument:
    return UploadedDocument(
        filename="test.pdf" if is_pdf else "test.jpg",
        mimetype="application/pdf" if is_pdf else "image/jpeg",
        content=b"data",
        is_pdf=is_pdf,
    )


def _file_upload(
    filename: str = "ticket.jpg",
    content: bytes = b"image-bytes",
    mimetype: str = "image/jpeg",
) -> FileStorage:
    return FileStorage(stream=io.BytesIO(content), filename=filename, content_type=mimetype)


async def test_health_ok(monkeypatch):
    monkeypatch.setattr(app_factory.client.models, "list", lambda: [SimpleNamespace(id="ok")])
    client = app_factory.create_app().test_client()

    resp = await client.get("/v1/health")

    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["status"] == "healthy"
    assert body["runtime"]["ocr"]["max_concurrency"] >= 1


async def test_ocr_missing_file_returns_standard_error():
    client = app_factory.create_app().test_client()

    resp = await client.post("/v1/ocr")

    assert resp.status_code == 400
    body = await resp.get_json()
    assert body["code"] == "missing_file"
    assert "error" in body


async def test_ocr_missing_file_logs_api_error(monkeypatch):
    captured = {}

    def _warning(message, *args):
        captured["message"] = message
        captured["args"] = args

    monkeypatch.setattr(app_factory.logger, "warning", _warning)
    client = app_factory.create_app().test_client()

    resp = await client.post("/v1/ocr")

    assert resp.status_code == 400
    assert captured["message"] == "api_error id=%s status=%s code=%s message=%s context=%s details=%s"
    assert isinstance(captured["args"][0], str)
    assert len(captured["args"][0]) >= 8
    assert captured["args"][1] == 400
    assert captured["args"][2] == "missing_file"
    assert captured["args"][3] == "No file provided"
    assert captured["args"][4]["path"] == "/v1/ocr"
    assert captured["args"][4]["method"] == "POST"


async def test_ocr_logs_request_lifecycle_and_sets_request_id_header(monkeypatch):
    info_logs = []

    def _info(message, *args):
        info_logs.append((message, args))

    monkeypatch.setattr(app_factory.logger, "info", _info)
    monkeypatch.setattr(
        app_factory.ocr_service,
        "process_document",
        lambda _doc: {
            "texto_extraido": "TEXTO OCR",
            "needs_retry": False,
            "retry_reasons": [],
            "usage": Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        },
    )
    client = app_factory.create_app().test_client()

    resp = await client.post(
        "/v1/ocr",
        files={"file": _file_upload()},
        headers={"X-Request-Id": "bot-request-123"},
    )

    assert resp.status_code == 200
    assert resp.headers["X-Request-Id"] == "bot-request-123"
    logged_messages = [message for message, _args in info_logs]
    assert "request_started id=%s context=%s" in logged_messages
    assert "document_parsed source=%s filename=%s mimetype=%s bytes=%s is_pdf=%s" in logged_messages
    assert "ocr_document_received id=%s filename=%s mimetype=%s bytes=%s is_pdf=%s" in logged_messages
    assert (
        "ocr_processed id=%s filename=%s is_pdf=%s elapsed_ms=%s state=%s retry=%s retry_reasons=%s tokens=%s text_len=%s"
        in logged_messages
    )
    assert "request_finished id=%s status=%s elapsed_ms=%s response_length=%s" in logged_messages


async def test_ocr_logs_debug_response_payload_when_enabled(monkeypatch):
    info_logs = []

    def _info(message, *args):
        info_logs.append((message, args))

    monkeypatch.setattr(ocr_core, "OCR_LOG_CONTENT", True)
    monkeypatch.setattr(ocr_core, "OCR_LOG_TEXT_LIMIT", 4000)
    monkeypatch.setattr(app_factory.logger, "info", _info)
    monkeypatch.setattr(
        app_factory.ocr_service,
        "process_document",
        lambda _doc: {
            "texto_extraido": "TEXTO OCR",
            "campos": {"numero_documento": "123456", "total": "25.00"},
            "needs_retry": False,
            "retry_reasons": [],
            "usage": Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        },
    )
    client = app_factory.create_app().test_client()

    resp = await client.post(
        "/v1/ocr",
        files={"file": _file_upload()},
    )

    assert resp.status_code == 200
    debug_logs = [args[1] for message, args in info_logs if message == "%s payload=%s" and args[0] == "ocr.debug.api_response"]
    assert len(debug_logs) == 1
    assert '"numero_documento": "123456"' in debug_logs[0]
    assert '"texto_extraido": "TEXTO OCR"' in debug_logs[0]


async def test_ocr_accepts_multipart_upload(monkeypatch):
    captured = {}

    def _process(doc):
        captured["document"] = doc
        return {
            "texto_extraido": "TEXTO OCR",
            "needs_retry": False,
            "retry_reasons": [],
            "usage": Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        }

    monkeypatch.setattr(app_factory.ocr_service, "process_document", _process)
    client = app_factory.create_app().test_client()

    resp = await client.post(
        "/v1/ocr",
        files={"file": _file_upload()},
    )

    assert resp.status_code == 200
    assert captured["document"].filename == "ticket.jpg"
    assert captured["document"].mimetype == "image/jpeg"
    assert captured["document"].content == b"image-bytes"
    assert captured["document"].is_pdf is False


async def test_ocr_accepts_json_base64_payload(monkeypatch):
    captured = {}

    def _process(doc):
        captured["document"] = doc
        return {
            "texto_extraido": "TEXTO OCR",
            "needs_retry": False,
            "retry_reasons": [],
            "usage": Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        }

    monkeypatch.setattr(app_factory.ocr_service, "process_document", _process)
    client = app_factory.create_app().test_client()

    resp = await client.post(
        "/v1/ocr",
        json={
            "image_base64": base64.b64encode(b"image-bytes").decode("ascii"),
            "filename": "ticket.png",
            "mimetype": "image/png",
        },
    )

    assert resp.status_code == 200
    assert captured["document"].filename == "ticket.png"
    assert captured["document"].mimetype == "image/png"
    assert captured["document"].content == b"image-bytes"
    assert captured["document"].is_pdf is False


async def test_ocr_accepts_binary_body_payload(monkeypatch):
    captured = {}

    def _process(doc):
        captured["document"] = doc
        return {
            "texto_extraido": "TEXTO OCR",
            "needs_retry": False,
            "retry_reasons": [],
            "usage": Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        }

    monkeypatch.setattr(app_factory.ocr_service, "process_document", _process)
    client = app_factory.create_app().test_client()

    resp = await client.post(
        "/v1/ocr",
        data=b"image-bytes",
        headers={"Content-Type": "image/jpeg", "X-Filename": "ticket.jpg"},
    )

    assert resp.status_code == 200
    assert captured["document"].filename == "ticket.jpg"
    assert captured["document"].mimetype == "image/jpeg"
    assert captured["document"].content == b"image-bytes"
    assert captured["document"].is_pdf is False


async def test_ocr_invalid_json_base64_returns_standard_error():
    client = app_factory.create_app().test_client()

    resp = await client.post(
        "/v1/ocr",
        json={"image_base64": "%%%invalid%%%"},
    )

    assert resp.status_code == 400
    body = await resp.get_json()
    assert body["code"] == "invalid_base64"


async def test_ocr_success_response_contract(monkeypatch):
    monkeypatch.setattr(app_factory, "read_uploaded_document", lambda _req: _dummy_document(is_pdf=False))
    monkeypatch.setattr(
        app_factory.ocr_service,
        "process_document",
        lambda _doc: {
            "texto_extraido": "TEXTO OCR",
            "needs_retry": False,
            "retry_reasons": [],
            "usage": Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        },
    )

    client = app_factory.create_app().test_client()
    resp = await client.post("/v1/ocr")

    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["estado"] == "ok"
    assert body["debe_reintentar"] is False
    assert body["texto_extraido"] == "TEXTO OCR"
    assert body["uso"]["total_tokens"] == 15
    assert "costo_estimado" in body


async def test_ocr_notifies_gonet_bot_on_retry(monkeypatch):
    captured = {}

    def _fake_send(payload):
        captured["payload"] = payload
        return 200, "ok"

    monkeypatch.setattr(app_factory, "GONET_BOT_SEND_URL", "http://gonet.test/send")
    monkeypatch.setattr(app_factory, "read_uploaded_document", lambda _req: _dummy_document(is_pdf=False))
    monkeypatch.setattr(app_factory, "_send_gonet_bot_message", _fake_send)
    monkeypatch.setattr(
        app_factory.ocr_service,
        "process_document",
        lambda _doc: {
            "texto_extraido": "OCR INCIERTO",
            "campos": {
                "entidad_bancaria": "JUVENTUD ECUATORIANA PROGRESISTA",
                "numero_documento": "40**010**309",
                "fecha": "29-11-2024",
                "hora": "20:10:03.747",
                "total": "USD 25.00",
                "nombre_depositante": "LOJAN CABRERA JUAN CARLOS",
                "ci_ruc": "0706626777",
            },
            "needs_retry": True,
            "retry_reasons": ["campos_criticos_sospechosos"],
            "usage": Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        },
    )

    client = app_factory.create_app().test_client()
    resp = await client.post(
        "/v1/ocr",
        form={
            "recipient": "593999000111",
            "chanel": "whatsapp",
            "cedula": "0706626777",
        },
    )

    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["debe_reintentar"] is True
    assert captured["payload"]["recipient"] == "593999000111"
    assert captured["payload"]["chanel"] == "whatsapp"
    assert captured["payload"]["origen"] == "ia"
    assert captured["payload"]["cedula"] == "0706626777"
    assert '["Asistencia Humana"]' in captured["payload"]["message"]
    assert "campos críticos sospechosos" in captured["payload"]["message"]


async def test_ocr_keeps_response_ok_when_gonet_handoff_fails(monkeypatch):
    monkeypatch.setattr(app_factory, "GONET_BOT_SEND_URL", "http://gonet.test/send")
    monkeypatch.setattr(app_factory, "read_uploaded_document", lambda _req: _dummy_document(is_pdf=False))

    def _boom(_payload):
        raise RuntimeError("handoff boom")

    monkeypatch.setattr(app_factory, "_send_gonet_bot_message", _boom)
    monkeypatch.setattr(
        app_factory.ocr_service,
        "process_document",
        lambda _doc: {
            "texto_extraido": "OCR INCIERTO",
            "campos": {"entidad_bancaria": "BANCO PICHINCHA", "numero_documento": "123456", "total": "25.00"},
            "needs_retry": True,
            "retry_reasons": ["campos_criticos_sospechosos"],
            "usage": Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        },
    )

    client = app_factory.create_app().test_client()
    resp = await client.post(
        "/v1/ocr",
        form={"recipient": "593999000111", "chanel": "whatsapp"},
    )

    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["debe_reintentar"] is True
    assert body["estado"] == "reintentar_foto"


def test_ocr_structured_openai_logs_usage_payload(monkeypatch):
    info_logs = []

    def _info(message, *args):
        info_logs.append((message, args))

    fake_response = SimpleNamespace(
        output_text='{"raw_text":"TEXTO OCR","fields":{"numero_documento":"123456","codigo_cnb":null,"nombre_depositante":null,"ci_ruc":null,"entidad_bancaria":"BANCO PICHINCHA","fecha":null,"hora":null,"total":"23.00"}}',
        usage=SimpleNamespace(input_tokens=12, output_tokens=7, total_tokens=19),
    )

    monkeypatch.setattr(ocr_core, "OCR_LOG_CONTENT", True)
    monkeypatch.setattr(ocr_core.logger, "info", _info)
    monkeypatch.setattr(ocr_core.client.responses, "create", lambda **_kwargs: fake_response)

    parsed, usage = ocr_core._ocr_structured_openai(
        b"image-bytes",
        detail="high",
        trace_id="trace-1",
        variant_name="original",
    )

    assert parsed is not None
    assert parsed["fields"]["numero_documento"] == "123456"
    assert usage.total_tokens == 19
    debug_logs = [
        args[1]
        for message, args in info_logs
        if message == "%s payload=%s" and args[0] == "ocr.debug.openai_response"
    ]
    assert len(debug_logs) == 1
    assert '"total_tokens": 19' in debug_logs[0]
    assert '"numero_documento": "123456"' in debug_logs[0]


async def test_ocr_processing_failure_returns_api_error(monkeypatch):
    monkeypatch.setattr(app_factory, "read_uploaded_document", lambda _req: _dummy_document(is_pdf=False))

    def _boom(_doc):
        raise RuntimeError("boom")

    monkeypatch.setattr(app_factory.ocr_service, "process_document", _boom)

    client = app_factory.create_app().test_client()
    resp = await client.post("/v1/ocr")

    assert resp.status_code == 500
    body = await resp.get_json()
    assert body["code"] == "ocr_processing_failed"


async def test_ocr_tesseract_pdf_not_supported(monkeypatch):
    monkeypatch.setattr(app_factory, "read_uploaded_document", lambda _req: _dummy_document(is_pdf=True))
    client = app_factory.create_app().test_client()

    resp = await client.post("/v1/ocr-tesseract")

    assert resp.status_code == 400
    body = await resp.get_json()
    assert body["code"] == "pdf_not_supported"


async def test_bounded_executor_runs_multiple_tasks_in_parallel():
    executor = BoundedExecutor(
        name="test-parallel",
        max_concurrency=2,
        queue_timeout_seconds=0.2,
        default_timeout_seconds=1.0,
        busy_message="busy",
        busy_code="service_busy",
        busy_status=429,
        timeout_message="timeout",
        timeout_code="processing_timeout",
        timeout_status=504,
    )
    active = 0
    max_seen = 0
    lock = threading.Lock()

    def _work() -> str:
        nonlocal active, max_seen
        with lock:
            active += 1
            max_seen = max(max_seen, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return "ok"

    try:
        results = await asyncio.gather(executor.run(_work), executor.run(_work))
    finally:
        executor.shutdown()

    assert results == ["ok", "ok"]
    assert max_seen == 2


async def test_bounded_executor_rejects_when_queue_timeout_is_exceeded():
    executor = BoundedExecutor(
        name="test-busy",
        max_concurrency=1,
        queue_timeout_seconds=0.02,
        default_timeout_seconds=1.0,
        busy_message="busy",
        busy_code="service_busy",
        busy_status=429,
        timeout_message="timeout",
        timeout_code="processing_timeout",
        timeout_status=504,
    )
    started = threading.Event()
    release = threading.Event()

    def _blocked_work() -> None:
        started.set()
        release.wait(timeout=1.0)

    first = asyncio.create_task(executor.run(_blocked_work))
    await asyncio.to_thread(started.wait, 0.1)

    try:
        assert executor.queue_timeout_seconds == 0.1
        with pytest.raises(APIError) as exc_info:
            await executor.run(time.sleep, 0.01)
    finally:
        release.set()
        await first
        executor.shutdown()

    assert exc_info.value.code == "service_busy"
    assert exc_info.value.status_code == 429


async def test_bounded_executor_times_out_long_running_task():
    executor = BoundedExecutor(
        name="test-timeout",
        max_concurrency=1,
        queue_timeout_seconds=1.0,
        default_timeout_seconds=0.02,
        busy_message="busy",
        busy_code="service_busy",
        busy_status=429,
        timeout_message="timeout",
        timeout_code="processing_timeout",
        timeout_status=504,
    )
    release = threading.Event()

    def _slow_work() -> None:
        release.wait(timeout=1.0)

    try:
        assert executor.default_timeout_seconds == 0.1
        with pytest.raises(APIError) as exc_info:
            await executor.run(_slow_work)
    finally:
        release.set()
        executor.shutdown()

    assert exc_info.value.code == "processing_timeout"
    assert exc_info.value.status_code == 504


def test_docnum_soft_majority_accepts_single_digit_outlier():
    assert ocr_core._docnum_has_soft_majority(["030522", "030527", "030522"]) is True


def test_docnum_soft_majority_accepts_single_char_truncation_outlier():
    assert ocr_core._docnum_has_soft_majority(["030522", "03052?", "030522"]) is True


def test_docnum_majority_value_accepts_repeated_doc_over_singletons():
    assert ocr_core._docnum_majority_value(["8962366", "89623366", "8962366", "8982336"]) == "8962366"


def test_doc_candidates_support_expected_accepts_small_ocr_variation():
    assert ocr_core._doc_candidates_support_expected("8962366", ["9962366", "82346"]) is True


def test_docnum_soft_majority_rejects_large_or_multiway_conflict():
    assert ocr_core._docnum_has_soft_majority(["88448335", "864935", "8244959"]) is False
    assert ocr_core._docnum_has_soft_majority(["030522", "030527", "030528"]) is False


def test_should_retry_for_jep_text_mismatch_detects_hallucinated_jep_ticket():
    local_text = (
        "COOPERATIVA JEP LTDA.\n"
        "CTA 406144745900 AHORROSIEP\n"
        "REALIZADO POR:\n"
        "DEP. EFECTIVO\n"
        "BALANCE OK\n"
    )
    openai_text = (
        "CLUB FITNESS ECO L.P.C.\n"
        "Deposito de venta de Farmacia la\n"
        "JEP, con el monto recibido.\n"
        "CTA 0104-07074530 07221899\n"
        "DEP. EFECTIVO 7.50\n"
    )

    assert ocr_core._should_retry_for_jep_text_mismatch(openai_text, local_text) is True


def test_should_retry_for_jep_text_mismatch_ignores_consistent_jep_ticket():
    local_text = (
        "COOPERATIVA JEP LTDA.\n"
        "CTA 406144745900 AHORROSJEP\n"
        "REALIZADO POR:\n"
        "DEP. EFECTIVO\n"
        "BALANCE OK\n"
    )
    openai_text = (
        "COOPERATIVA JEP LTDA.\n"
        "CTA 406144746900 AHORROSJEP\n"
        "REALIZADO POR:\n"
        "DEP. EFECTIVO\n"
        "BALANCE OK\n"
    )

    assert ocr_core._should_retry_for_jep_text_mismatch(openai_text, local_text) is False


def test_extract_cnb_candidates_supports_cmb_ocr_confusion():
    text = "CMB.........: 0104089313001"

    assert ocr_core._extract_cnb_candidates_from_text(text) == ["0104089313001"]


def test_extract_docnum_candidates_supports_transaction_label():
    text = "Fecha de pago 12 abr 2026 - 08h34\nNro. de transacción 759185347061\n"

    assert ocr_core._extract_docnum_candidates_from_text(text) == ["759185347061"]


def test_docnum_has_label_evidence_accepts_transaction_reference():
    text = "Pagaste a GONET\nNro. de transacción 759185347061\n"

    assert ocr_core._docnum_has_label_evidence(text, "759185347061", strict_labels=True) is True


def test_regex_extract_core_uses_transaction_reference_as_document_number():
    text = (
        "Pagaste a GONET\n"
        "Total pagado $17,19\n"
        "Fecha de pago 12 abr 2026 - 08h34\n"
        "Nro. de transacción 759185347061\n"
    )

    fields = ocr_core._regex_extract_core(text)

    assert fields["numero_documento"] == "759185347061"


def test_replace_cnb_in_text_repairs_label_and_digits():
    raw_text = "Banco\nCMB.........: 0104489313001\nFecha.......: 2024/nov./30 10:33"

    fixed = ocr_core._replace_cnb_in_text(raw_text, "0104089313001")

    assert "CNB.........: 0104089313001" in fixed


def test_missing_or_suspicious_requires_cnb_for_pichincha_recaudaciones():
    fields = {
        "numero_documento": "030522",
        "entidad_bancaria": "BANCO PICHINCHA",
        "fecha": "2024/nov./30",
        "hora": "10:33",
        "total": "27.95",
    }
    raw_text = (
        "BANCO PICHINCHA C.A.\n"
        "RECAUDACIONES\n"
        "Documento...: 030522\n"
        "Total.......: 27.95\n"
    )

    validation = ocr_core._missing_or_suspicious(fields, raw_text)

    assert "codigo_cnb" in validation["missing"]


def test_missing_or_suspicious_flags_cooperative_bank_without_prefix():
    fields = {
        "numero_documento": "40**010**309",
        "entidad_bancaria": "JUVENTUD ECUATORIANA PROGRESISTA",
        "fecha": "29-11-2024",
        "hora": "20:10:03.747",
        "total": "25.00",
    }
    raw_text = (
        "COOPERATIVA\n"
        "JUVENTUD ECUATORIANA PROGRESISTA\n"
        "FECHA : 29-11-2024 20:10:03.747\n"
        "CUENTA : 40**010**309\n"
        "VALOR DEPOSITADO : USD 25.00\n"
    )

    validation = ocr_core._missing_or_suspicious(fields, raw_text)

    assert "entidad_bancaria" in validation["suspicious"]


def test_is_pichincha_deposito_ticket_detects_branch_deposit_receipt():
    fields = {"entidad_bancaria": "BANCO PICHINCHA"}
    raw_text = (
        "BANCO PICHINCHA C.A.\n"
        "DEPOSITO\n"
        "CUENTA CORRIENTE\n"
        "Nombre CNB: TIENDA DE ABARROTES\n"
        "RUC CNB...: 0301517819001\n"
        "Documento.: 099769\n"
        "Control...: 165530500\n"
    )

    assert ocr_core._is_pichincha_deposito_ticket(fields, raw_text) is True


def test_normalize_textual_month_token_accepts_noisy_ene():
    assert ocr_core._normalize_textual_month_token("0n0") == "ene"
    assert ocr_core._normalize_textual_month_token("one.") == "ene"
    assert ocr_core._normalize_textual_month_token("elni") == "ene"
    assert ocr_core._normalize_textual_month_token("eliv") == "ene"


def test_build_textual_month_date_replaces_numeric_month():
    assert ocr_core._build_textual_month_date("2026/10/20", "ene") == "2026/ene./20"


def test_normalize_textual_month_in_date_repairs_noisy_month():
    assert ocr_core._normalize_textual_month_in_date("2026/elni./19") == "2026/ene./19"


def test_missing_or_suspicious_flags_numeric_month_for_pichincha_deposito():
    fields = {
        "numero_documento": "212619",
        "entidad_bancaria": "BANCO PICHINCHA",
        "fecha": "2026/10/20",
        "hora": "17:39",
        "total": "22.86",
    }
    raw_text = (
        "BANCO PICHINCHA C.A.\n"
        "DEPOSITO\n"
        "CUENTA CORRIENTE\n"
        "Documento.: 212619\n"
        "Fecha.....: 2026/10/20 17:39\n"
    )

    validation = ocr_core._missing_or_suspicious(fields, raw_text)

    assert "fecha" in validation["suspicious"]


def test_extract_ruc_cnb_candidates_supports_noisy_separator():
    text = "RUC CNB...: 0704607:118001"

    assert ocr_core._extract_ruc_cnb_candidates_from_text(text) == ["0704607118001"]


def test_replace_ruc_cnb_in_text_repairs_digits():
    raw_text = "Nombre CNB: KESOCELI. PRO\nRUC CMB...: 0704601'18001\nFecha.....: 2026/ene./19 12:11"

    fixed = ocr_core._replace_ruc_cnb_in_text(raw_text, "0704607118001")

    assert "RUC CNB...: 0704607118001" in fixed


def test_extract_control_candidates_from_text_reads_digits():
    text = "Control...: 152854103"

    assert ocr_core._extract_control_candidates_from_text(text) == ["152854103"]


def test_replace_control_in_text_repairs_digits():
    raw_text = "Documento.: 022238\nControl...: 15285403\nFecha.....: 2026/ene./19 12:11"

    fixed = ocr_core._replace_control_in_text(raw_text, "152854103")

    assert "Control...: 152854103" in fixed


def test_assess_pichincha_numeric_consistency_requires_retry_for_tiny_unverified_ticket():
    best_raw_text = (
        "BANCO PICHINCHA C.A\n"
        "DEPOSITO\n"
        "CUENTA CORRIENTE\n"
        "RUC CNB...: 0704607118001\n"
        "Control...: 15285403\n"
    )
    top_trace = [
        {"ruc_cnb": "0704607118001", "control": "15285403"},
        {"ruc_cnb": "", "control": ""},
        {"ruc_cnb": "", "control": ""},
    ]

    fixed_text, needs_retry, reasons = ocr_core._assess_pichincha_numeric_consistency(best_raw_text, top_trace, (410, 459))

    assert fixed_text == best_raw_text
    assert needs_retry is True
    assert "campos_numericos_inconsistentes_ocr" in reasons


def test_should_relax_pichincha_deposito_docnum_retry_for_clear_ticket():
    fields = {
        "entidad_bancaria": "Banco Pichincha",
        "numero_documento": "77200866",
        "fecha": "2026-04-13",
        "total": "16.76",
    }
    raw_text = (
        "BANCO PICHINCHA\n"
        "DEPOSITO\n"
        "CUENTA CORRIENTE\n"
        "N° de comprobante 77200866\n"
    )
    validation = {"missing": [], "suspicious": []}

    assert (
        ocr_core._should_relax_pichincha_deposito_docnum_retry(
            fields,
            raw_text,
            validation,
            "77200866",
        )
        is True
    )


def test_retry_decision_forces_retry_on_extreme_blur_even_with_strong_fields():
    quality = QualityMetrics(
        blur_score=4.5,
        blur_threshold=ocr_core.BLUR_THRESHOLD,
        is_blurry=True,
        brightness_mean=182.0,
        edge_density=0.00001,
        resolution=(1081, 1920),
        is_screen_capture=False,
    )
    validation = {
        "core": {
            "numero_documento": "171358",
            "entidad_bancaria": "BANCO PICHINCHA",
            "fecha": "2026/ene./19",
            "hora": "07:34",
            "total": "25.59",
        },
        "missing": [],
        "suspicious": [],
    }
    text = (
        "BANCO PICHINCHA C.A.\n"
        "DEPOSITO\n"
        "Documento.: 171358\n"
        "Fecha.....: 2026/ene./19 07:34\n"
        "Total.....: 25.59\n"
    )

    needs_retry, reasons = ocr_core._retry_decision(quality, validation, 336, text)

    assert needs_retry is True
    assert "imagen_demasiado_borrosa" in reasons


def test_looks_like_pacifico_portal_report_detects_photographed_report():
    openai_text = (
        "Banco del Pacifico\n"
        "Cliente Nro: 000230357      Suc. Banco: 5      150403521\n"
        "Valor autorizado: 2023-01-02\n"
        "Fecha Inicio Pago: 2026-01-14\n"
        "Fecha Vencimiento: 2026-01-22\n"
        "Cuenta Debito: 10-51223430      Estado: APROBADO\n"
        "Cuenta      Concepto      Monto adeudado      Moneda      Valor      Estado\n"
        "CONCEPTOS LUGAR INFORMACION\n"
        "CREACION\n"
        "TRAMITADO\n"
        "CERTIFICACION BANCO PACIFICO\n"
        "USUARIO\n"
        "FECHA/HORA\n"
    )
    local_text = "Banco del Pacifico\nCERTIFICACION BANCO PACIFICO\nUSUARIO\nFECHA/HORA\n"

    assert ocr_core._looks_like_pacifico_portal_report(openai_text, local_text, 40) is True


def test_looks_like_pacifico_portal_report_ignores_normal_receipt():
    openai_text = (
        "Banco del Pacifico\n"
        "Transferencia exitosa\n"
        "Comprobante: 123456\n"
        "Monto: 22.89\n"
        "Fecha: 2026-01-14 08:56:45\n"
    )

    assert ocr_core._looks_like_pacifico_portal_report(openai_text, "", 120) is False


def test_looks_like_pacifico_portal_report_detects_actual_report_style_output():
    openai_text = (
        "Banco del Pacifico\n"
        "Cuenta 082100749523\n"
        "Comprobante POSTAL DEL ESTADO INT DEC 2023 TC 0850484\n"
        "Deposito 9204238493\n"
        "Fecha Inicio Pago: 2023-01-14\n"
        "Estado APROBADO\n"
        "CONCEPTOS CANCELADOS\n"
        "Cuenta Concepto Descripcion\n"
        "Creacion PETIGANDRIAS 2023-01-14 02:38:45\n"
        "Ruta:1 PETIGANDRIAS 2023/01/14 02:39:49\n"
        "TRANSMITIDO PETIGANDRIAS 2023-01-14-\n"
        "CERTIFICACION BANCO PACIFICO 2023-01-14\n"
    )

    assert ocr_core._looks_like_pacifico_portal_report(openai_text, "", 40) is True
