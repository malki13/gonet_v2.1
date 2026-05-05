import inspect
import json
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from quart import Quart, g, jsonify, request
from werkzeug.exceptions import HTTPException

from ..domain.models import APIError
from ..services.ocr_core import (
    DATASET_DEFAULT_DIR,
    MAX_UPLOAD_BYTES,
    OPENAI_ANALYZE_DETAIL,
    OPENAI_OCR_DETAIL,
    OPENAI_OCR_MODEL,
    TOP_VARIANTS,
    add_usage,
    build_ocr_api_response,
    client,
    empty_usage,
    estimate_cost,
    extract_bank_from_text,
    list_dataset_files,
    log_debug_payload,
    logger,
    ocr_service,
    parse_int_query_param,
    process_image_bytes,
    read_uploaded_document,
)
from ..services.runtime import service_runtime

app = Quart(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

GONET_BOT_SEND_URL = os.getenv("GONET_BOT_SEND_URL", "").strip()
GONET_BOT_TIMEOUT_SECONDS = max(1.0, float(os.getenv("GONET_BOT_TIMEOUT_SECONDS", "8")))
GONET_BOT_ORIGIN = os.getenv("GONET_BOT_ORIGIN", "ia").strip() or "ia"
DATASET_ANALYZE_TIMEOUT_SECONDS = max(30.0, float(os.getenv("DATASET_ANALYZE_TIMEOUT_SECONDS", "900")))


def _request_id() -> str:
    request_id = getattr(g, "request_id", None)
    if request_id:
        return request_id

    header_request_id = (request.headers.get("X-Request-Id") or "").strip()
    request_id = header_request_id or uuid.uuid4().hex[:12]
    g.request_id = request_id
    return request_id


def _is_form_request() -> bool:
    content_type = (request.content_type or "").strip().lower()
    return "multipart/form-data" in content_type or content_type.startswith("application/x-www-form-urlencoded")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _request_debug_context() -> dict[str, Any]:
    payload = await request.get_json(silent=True) if request.is_json else None
    if isinstance(payload, dict):
        json_keys = sorted(str(key) for key in payload.keys())
    elif payload is None:
        json_keys = []
    else:
        json_keys = [type(payload).__name__]

    files: list[str] = []
    form_keys: list[str] = []
    if _is_form_request():
        form = await request.form
        files = sorted((await request.files).keys())
        form_keys = sorted(form.keys())

    return {
        "method": request.method,
        "path": request.path,
        "content_type": request.content_type,
        "content_length": request.content_length,
        "files": files,
        "form": form_keys,
        "json_keys": json_keys,
        "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
    }


async def _request_payload_data() -> dict[str, Any]:
    if request.is_json:
        payload = await request.get_json(silent=True)
        if isinstance(payload, dict):
            return payload
        return {}
    if _is_form_request():
        form = await request.form
        if form:
            return form.to_dict(flat=True)
    return {}


def _parse_boolish(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


async def _request_handoff_context() -> dict[str, Any]:
    payload = await _request_payload_data()
    return {
        "recipient": str(payload.get("recipient") or request.headers.get("X-Recipient") or "").strip(),
        "channel": str(
            payload.get("chanel")
            or payload.get("channel")
            or request.headers.get("X-Chanel")
            or request.headers.get("X-Channel")
            or ""
        )
        .strip()
        .lower(),
        "cedula": str(
            payload.get("cedula")
            or payload.get("identificacion")
            or request.headers.get("X-Cedula")
            or ""
        ).strip(),
        "force_handoff": _parse_boolish(
            payload.get("force_human_handoff")
            or payload.get("escalar_humano")
            or request.headers.get("X-Human-Handoff"),
            default=False,
        ),
        "notify_gonet": _parse_boolish(
            payload.get("notify_gonet_bot")
            or payload.get("notificar_gonet")
            or request.headers.get("X-Notify-Gonet-Bot"),
            default=True,
        ),
    }


def _clean_summary_value(value: Any) -> str:
    cleaned = " ".join(str(value or "").split())
    return cleaned.replace("[", "(").replace("]", ")")


def _humanize_retry_reason(reason: str) -> str:
    labels = {
        "campos_criticos_sospechosos": "campos críticos sospechosos",
        "campos_numericos_inconsistentes_ocr": "campos numéricos inconsistentes",
        "faltan_campos_criticos": "faltan campos críticos",
        "foto_de_pantalla": "captura de pantalla inválida",
        "imagen_borrosa_o_movida": "imagen borrosa o movida",
        "imagen_demasiado_borrosa": "imagen demasiado borrosa",
        "imagen_muy_brillante": "imagen muy brillante",
        "imagen_muy_oscura": "imagen muy oscura",
        "inconsistencia_ocr_entre_variantes": "inconsistencia OCR entre variantes",
        "inconsistencia_texto_con_ocr_local": "inconsistencia frente al OCR local",
        "numero_documento_baja_confianza": "número de comprobante con baja confianza",
        "numero_documento_inconsistente_ocr": "número de comprobante inconsistente",
        "ocr_no_estructurado": "OCR sin estructura confiable",
        "pdf_invalido": "PDF inválido",
        "pdf_sin_paginas": "PDF sin páginas válidas",
        "poco_detalle_en_texto": "poco detalle en el texto",
        "resolucion_baja": "resolución baja",
        "texto_insuficiente": "texto insuficiente",
    }
    return labels.get(reason, reason.replace("_", " "))


def _build_handoff_summary(request_id: str, document: Any, payload: dict[str, Any], response: dict[str, Any]) -> str:
    fields = payload.get("campos", {}) if isinstance(payload.get("campos"), dict) else {}
    reasons = response.get("motivos_reintento", []) if isinstance(response.get("motivos_reintento"), list) else []

    summary_lines = [
        (
            "No pudimos validar automaticamente este comprobante y lo derivamos a un asesor humano."
            if response.get("debe_reintentar")
            else "Derivamos este comprobante a un asesor humano para revision manual."
        ),
        "Resumen detectado:",
    ]

    field_lines = [
        ("Archivo", getattr(document, "filename", "")),
        ("Entidad", fields.get("entidad_bancaria")),
        ("Numero de documento", fields.get("numero_documento")),
        ("Codigo CNB", fields.get("codigo_cnb")),
        ("Fecha", fields.get("fecha")),
        ("Hora", fields.get("hora")),
        ("Total", fields.get("total")),
        ("Depositante", fields.get("nombre_depositante")),
        ("CI/RUC", fields.get("ci_ruc")),
    ]
    for label, value in field_lines:
        cleaned = _clean_summary_value(value)
        if cleaned:
            summary_lines.append(f"- {label}: {cleaned}")

    if reasons:
        summary_lines.append(f"- Motivos: {', '.join(_humanize_retry_reason(r) for r in reasons)}")

    summary_lines.append(f"- ID solicitud: {_clean_summary_value(request_id)}")
    summary_lines.append('["Asistencia Humana"]')
    return "\n".join(summary_lines)


def _send_gonet_bot_message(gonet_payload: dict[str, Any]) -> tuple[int, str]:
    body = json.dumps(gonet_payload).encode("utf-8")
    req = urlrequest.Request(
        GONET_BOT_SEND_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=GONET_BOT_TIMEOUT_SECONDS) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            resp_body = resp.read().decode("utf-8", errors="replace")
            return status, resp_body
    except urlerror.HTTPError as exc:
        resp_body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), resp_body


async def _notify_gonet_bot_handoff(
    request_id: str,
    document: Any,
    payload: dict[str, Any],
    response: dict[str, Any],
    handoff_context: dict[str, Any],
) -> dict[str, Any]:
    should_handoff = bool(response.get("debe_reintentar")) or bool(handoff_context.get("force_handoff"))
    if not should_handoff:
        return {"attempted": False, "sent": False, "reason": "not_needed"}
    if not handoff_context.get("notify_gonet", True):
        return {"attempted": False, "sent": False, "reason": "disabled_for_request"}
    if not GONET_BOT_SEND_URL:
        return {"attempted": False, "sent": False, "reason": "missing_send_url"}

    recipient = str(handoff_context.get("recipient") or "").strip()
    channel = str(handoff_context.get("channel") or "").strip().lower()
    if not recipient or channel not in {"whatsapp", "messenger"}:
        logger.warning(
            "gonet_handoff_skipped id=%s reason=%s recipient=%s channel=%s",
            request_id,
            "missing_routing_context",
            recipient or "<empty>",
            channel or "<empty>",
        )
        return {"attempted": False, "sent": False, "reason": "missing_routing_context"}

    summary = _build_handoff_summary(request_id, document, payload, response)
    gonet_payload = {
        "message": summary,
        "chanel": channel,
        "recipient": recipient,
        "origen": GONET_BOT_ORIGIN,
    }
    cedula = str(handoff_context.get("cedula") or "").strip()
    if cedula:
        gonet_payload["cedula"] = cedula

    log_debug_payload(
        "ocr.debug.gonet_handoff_request",
        {"id": request_id, "payload": gonet_payload},
    )
    try:
        status, response_body = await service_runtime.run_notify(
            _send_gonet_bot_message,
            gonet_payload,
            timeout_seconds=GONET_BOT_TIMEOUT_SECONDS + 1.0,
        )
    except Exception as exc:
        logger.warning(
            "gonet_handoff_failed id=%s recipient=%s channel=%s error=%s",
            request_id,
            recipient,
            channel,
            str(exc),
        )
        return {"attempted": True, "sent": False, "reason": str(exc)}

    sent = 200 <= status < 300
    log_debug_payload(
        "ocr.debug.gonet_handoff_response",
        {
            "id": request_id,
            "sent": sent,
            "status": status,
            "response": response_body,
        },
    )
    if sent:
        logger.info(
            "gonet_handoff_sent id=%s recipient=%s channel=%s status=%s",
            request_id,
            recipient,
            channel,
            status,
        )
    else:
        logger.warning(
            "gonet_handoff_failed id=%s recipient=%s channel=%s status=%s body=%s",
            request_id,
            recipient,
            channel,
            status,
            response_body,
        )
    return {"attempted": True, "sent": sent, "status": status}


def _analyze_dataset_sync(
    folder: Path,
    files: list[Path],
    variants_override: int | None,
    detail_param: str,
) -> dict[str, Any]:
    started = time.time()
    details = []
    usage_total = empty_usage()
    accepted = 0

    for path in files:
        try:
            image_bytes = path.read_bytes()
            payload = process_image_bytes(
                image_bytes,
                top_variants_override=variants_override,
                detail_override=detail_param,
            )
            usage_total = add_usage(usage_total, payload["usage"])

            needs_retry = bool(payload.get("needs_retry", True))
            if not needs_retry:
                accepted += 1

            details.append(
                {
                    "file": str(path),
                    "needs_retry": needs_retry,
                    "retry_reasons": payload.get("retry_reasons", []),
                    "score": payload.get("candidate_score", 0),
                    "detected_bank": extract_bank_from_text(
                        f"{payload.get('campos', {}).get('entidad_bancaria', '')}\n{payload.get('texto_extraido', '')}"
                    ),
                    "core_missing": payload.get("validation", {}).get("missing", []),
                    "core_suspicious": payload.get("validation", {}).get("suspicious", []),
                }
            )
        except Exception as exc:
            details.append(
                {
                    "file": str(path),
                    "needs_retry": True,
                    "retry_reasons": ["processing_exception"],
                    "error": str(exc),
                }
            )

    total = len(files)
    pass_rate = (accepted / total) * 100.0 if total else 0.0
    elapsed_ms = int((time.time() - started) * 1000)

    return {
        "folder": str(folder),
        "total": total,
        "accepted_without_retry": accepted,
        "retry_count": total - accepted,
        "pass_rate_percent": round(pass_rate, 2),
        "target_percent": 90.0,
        "target_met": pass_rate >= 90.0,
        "variants_used_per_image": variants_override if variants_override is not None else TOP_VARIANTS,
        "detail_used": detail_param,
        "elapsed_ms": elapsed_ms,
        "uso_total": asdict(usage_total),
        "costo_estimado_total": asdict(estimate_cost(usage_total)),
        "details": details,
    }


@app.before_request
async def log_request_started() -> None:
    g.request_started_at = time.perf_counter()
    logger.info("request_started id=%s context=%s", _request_id(), await _request_debug_context())


@app.after_request
async def log_request_finished(response: Any) -> Any:
    request_id = getattr(g, "request_id", None) or _request_id()
    started_at = getattr(g, "request_started_at", None)
    elapsed_ms = int((time.perf_counter() - started_at) * 1000) if started_at is not None else -1
    response.headers["X-Request-Id"] = request_id
    logger.info(
        "request_finished id=%s status=%s elapsed_ms=%s response_length=%s",
        request_id,
        response.status_code,
        elapsed_ms,
        getattr(response, "content_length", None),
    )
    return response


@app.errorhandler(APIError)
async def handle_api_error(exc: APIError) -> Any:
    body: dict[str, Any] = {"error": exc.message, "code": exc.code}
    if exc.details:
        body["details"] = exc.details
    logger.warning(
        "api_error id=%s status=%s code=%s message=%s context=%s details=%s",
        _request_id(),
        exc.status_code,
        exc.code,
        exc.message,
        await _request_debug_context(),
        exc.details or {},
    )
    return jsonify(body), exc.status_code


@app.errorhandler(Exception)
async def handle_unexpected_error(exc: Exception) -> Any:
    if isinstance(exc, HTTPException):
        return exc
    logger.exception("Unhandled exception id=%s context=%s", _request_id(), await _request_debug_context())
    return jsonify({"error": "Internal server error", "code": "internal_error"}), 500


@app.route("/v1/health", methods=["GET"])
@app.route("/health", methods=["GET"])
async def health() -> Any:
    openai_ok = True
    err = None
    try:
        await service_runtime.run_notify(client.models.list, timeout_seconds=15.0)
    except Exception as exc:
        openai_ok = False
        err = str(exc)

    return (
        jsonify(
            {
                "status": "healthy" if openai_ok else "degraded",
                "openai_api": "healthy" if openai_ok else "unhealthy",
                "model": OPENAI_OCR_MODEL,
                "error": err,
                "runtime": service_runtime.snapshot(),
                "limits": {"max_upload_bytes": MAX_UPLOAD_BYTES},
            }
        ),
        200 if openai_ok else 503,
    )


@app.route("/v1/ocr", methods=["POST"])
@app.route("/ocr", methods=["POST"])
async def ocr() -> Any:
    request_id = _request_id()
    handoff_context = await _request_handoff_context()
    document = await _maybe_await(read_uploaded_document(request))
    started = time.time()
    logger.info(
        "ocr_document_received id=%s filename=%s mimetype=%s bytes=%s is_pdf=%s",
        request_id,
        document.filename or "<empty>",
        document.mimetype or "<empty>",
        len(document.content),
        document.is_pdf,
    )

    try:
        payload = await service_runtime.run_ocr(ocr_service.process_document, document)
    except APIError:
        raise
    except Exception as exc:
        logger.exception("OCR processing failed")
        raise APIError(
            "OCR processing failed",
            status_code=500,
            code="ocr_processing_failed",
            details={"reason": str(exc)},
        ) from exc

    response = build_ocr_api_response(payload).to_dict()
    handoff_result = await _notify_gonet_bot_handoff(
        request_id=request_id,
        document=document,
        payload=payload,
        response=response,
        handoff_context=handoff_context,
    )
    log_debug_payload(
        "ocr.debug.api_response",
        {
            "id": request_id,
            "campos": payload.get("campos", {}),
            "gonet_handoff": handoff_result,
            "response": response,
        },
    )
    logger.info(
        "ocr_processed id=%s filename=%s is_pdf=%s elapsed_ms=%s state=%s retry=%s retry_reasons=%s tokens=%s text_len=%s",
        request_id,
        document.filename or "<empty>",
        document.is_pdf,
        int((time.time() - started) * 1000),
        response["estado"],
        response["debe_reintentar"],
        response["motivos_reintento"],
        response["uso"]["total_tokens"],
        len(response["texto_extraido"]),
    )
    return jsonify(response), 200


@app.route("/v1/ocr-tesseract", methods=["POST"])
@app.route("/ocr-tesseract", methods=["POST"])
async def ocr_tesseract() -> Any:
    request_id = _request_id()
    document = await _maybe_await(read_uploaded_document(request))
    logger.info(
        "ocr_tesseract_document_received id=%s filename=%s mimetype=%s bytes=%s is_pdf=%s",
        request_id,
        document.filename or "<empty>",
        document.mimetype or "<empty>",
        len(document.content),
        document.is_pdf,
    )
    if document.is_pdf:
        raise APIError("PDF is not supported in /ocr-tesseract", status_code=400, code="pdf_not_supported")

    payload = await service_runtime.run_ocr(ocr_service.process_tesseract_image, document.content)
    logger.info(
        "ocr_tesseract_processed id=%s filename=%s score=%s variant=%s text_len=%s",
        request_id,
        document.filename or "<empty>",
        payload.get("score", 0),
        payload.get("variant", ""),
        len(str(payload.get("texto_extraido", ""))),
    )
    return jsonify(payload), 200


@app.route("/v1/analyze-dataset", methods=["GET"])
@app.route("/analyze-dataset", methods=["GET"])
async def analyze_dataset() -> Any:
    folder_param = request.args.get("folder", DATASET_DEFAULT_DIR)
    limit_param = request.args.get("limit")
    variants_param = request.args.get("variants")
    detail_param = request.args.get("detail", OPENAI_ANALYZE_DETAIL)

    folder = Path(folder_param)
    if not folder.is_absolute():
        folder = (Path.cwd() / folder).resolve()

    if not folder.exists() or not folder.is_dir():
        raise APIError(
            f"Folder not found: {folder}",
            status_code=400,
            code="folder_not_found",
            details={"folder": str(folder)},
        )

    files = list_dataset_files(folder)
    if not files:
        raise APIError(
            f"No supported images in: {folder}",
            status_code=400,
            code="empty_dataset",
            details={"folder": str(folder)},
        )

    limit = parse_int_query_param(limit_param, "limit")
    if limit is not None:
        files = files[:limit]

    variants_override = parse_int_query_param(variants_param, "variants")
    report = await service_runtime.run_ocr(
        _analyze_dataset_sync,
        folder,
        files,
        variants_override,
        detail_param,
        timeout_seconds=DATASET_ANALYZE_TIMEOUT_SECONDS,
    )
    return jsonify(report), 200


def create_app() -> Quart:
    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("QUART_DEBUG", os.getenv("FLASK_DEBUG", "false")).lower() == "true"

    logger.info("Starting OCR service")
    logger.info("model=%s detail=%s", OPENAI_OCR_MODEL, OPENAI_OCR_DETAIL)
    logger.info("dataset_default_dir=%s", DATASET_DEFAULT_DIR)
    logger.info("runtime=%s", service_runtime.snapshot())

    app.run(host="0.0.0.0", port=port, debug=debug)
