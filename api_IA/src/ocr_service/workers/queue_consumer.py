import json
import logging
import os
import threading
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from ..domain.models import APIError
from ..services.ocr_core import (
    build_ocr_api_response,
    build_uploaded_document,
    decode_base64_document,
    log_uploaded_document,
    ocr_service,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("ocr_queue_consumer")

OCR_QUEUE_REDIS_URL = os.getenv("OCR_QUEUE_REDIS_URL") or os.getenv("REDIS_URL") or "redis://redis:6379/0"
OCR_QUEUE_NAME = os.getenv("OCR_QUEUE_NAME", "ocr:jobs").strip() or "ocr:jobs"
OCR_PROCESSING_QUEUE_NAME = os.getenv("OCR_PROCESSING_QUEUE_NAME", f"{OCR_QUEUE_NAME}:processing").strip()
OCR_QUEUE_BLOCK_SECONDS = max(1, int(os.getenv("OCR_QUEUE_BLOCK_SECONDS", "5")))
OCR_QUEUE_IDLE_SLEEP_SECONDS = max(0.5, float(os.getenv("OCR_QUEUE_IDLE_SLEEP_SECONDS", "2")))
OCR_JOB_MAX_ATTEMPTS = max(1, int(os.getenv("OCR_JOB_MAX_ATTEMPTS", "3")))
OCR_WORKER_THREADS = max(1, int(os.getenv("OCR_WORKER_THREADS", os.getenv("OCR_MAX_CONCURRENCY", "4"))))

OCR_CALLBACK_URL = os.getenv("OCR_CALLBACK_URL", "").strip()
OCR_CALLBACK_SECRET = os.getenv("OCR_CALLBACK_SECRET", "").strip()
OCR_CALLBACK_TIMEOUT_SECONDS = max(
    1.0,
    float(os.getenv("OCR_CALLBACK_TIMEOUT_SECONDS", os.getenv("OCR_HTTP_TIMEOUT_SECONDS", "30"))),
)


def _post_json(url: str, payload: dict[str, Any], *, timeout: float, headers: dict[str, str] | None = None) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", resp.getcode()))
            response_body = resp.read().decode("utf-8", errors="replace")
            return status, response_body
    except urlerror.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), response_body


def _parse_json_or_text(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {"status": "error", "raw_body": raw[:1000]}


def _job_attachment(job: dict[str, Any]) -> dict[str, Any]:
    attachments = job.get("attachments") or []
    if not attachments:
        raise APIError("No file provided", status_code=400, code="missing_file")
    attachment = attachments[0] or {}
    if not isinstance(attachment, dict):
        raise APIError("Invalid attachment payload", status_code=400, code="missing_file")
    return attachment


def _build_job_document(job: dict[str, Any]) -> Any:
    attachment = _job_attachment(job)
    encoded = attachment.get("base64_data")
    if encoded is None:
        encoded = attachment.get("file_base64")
    if encoded is None:
        raise APIError("No file provided", status_code=400, code="missing_file")
    if not isinstance(encoded, str):
        raise APIError("Base64 document must be a string", status_code=400, code="invalid_base64")

    content, inline_mimetype = decode_base64_document(encoded)
    filename = str(attachment.get("filename") or "").strip()
    mimetype = str(attachment.get("mime_type") or inline_mimetype).strip().lower()
    document = build_uploaded_document(content=content, filename=filename, mimetype=mimetype)
    log_uploaded_document("queue_base64", document)
    return document


def _serialize_api_error(exc: APIError) -> tuple[int, str]:
    body: dict[str, Any] = {"error": exc.message, "code": exc.code}
    if exc.details:
        body["details"] = exc.details
    return exc.status_code, json.dumps(body, ensure_ascii=False)


def _run_ocr_job(job: dict[str, Any]) -> dict[str, Any]:
    job_id = job.get("job_id")
    session_id = job.get("session_id")
    document = _build_job_document(job)
    started = time.perf_counter()
    logger.info(
        "ocr_queue_job_processing_started job_id=%s session_id=%s filename=%s mimetype=%s bytes=%s is_pdf=%s",
        job_id,
        session_id,
        document.filename or "<empty>",
        document.mimetype or "<empty>",
        len(document.content),
        document.is_pdf,
    )

    payload = ocr_service.process_document(document)
    response = build_ocr_api_response(payload).to_dict()

    logger.info(
        "ocr_queue_job_processing_done job_id=%s session_id=%s filename=%s elapsed_ms=%s state=%s retry=%s tokens=%s text_len=%s",
        job_id,
        session_id,
        document.filename or "<empty>",
        int((time.perf_counter() - started) * 1000),
        response["estado"],
        response["debe_reintentar"],
        response["uso"]["total_tokens"],
        len(response["texto_extraido"]),
    )
    return response


def _callback_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if OCR_CALLBACK_SECRET:
        headers["X-OCR-Callback-Secret"] = OCR_CALLBACK_SECRET
    return headers


def _ack(redis_client: Redis, raw: str) -> None:
    redis_client.lrem(OCR_PROCESSING_QUEUE_NAME, 1, raw)


def _requeue(redis_client: Redis, raw: str, job: dict[str, Any]) -> None:
    _ack(redis_client, raw)
    redis_client.lpush(OCR_QUEUE_NAME, json.dumps(job))


def _dead_letter(redis_client: Redis, raw: str, job: dict[str, Any], reason: str) -> None:
    logger.error(
        "ocr_queue_job_dead_letter job_id=%s session_id=%s reason=%s",
        job.get("job_id"),
        job.get("session_id"),
        reason,
    )
    _ack(redis_client, raw)


def _process_job(redis_client: Redis, raw: str) -> None:
    job = json.loads(raw)
    attempts = int(job.get("worker_attempts") or 0) + 1
    job["worker_attempts"] = attempts
    job_id = job.get("job_id")
    session_id = job.get("session_id")

    logger.info("ocr_queue_job_received job_id=%s session_id=%s attempt=%s", job_id, session_id, attempts)

    if not OCR_CALLBACK_URL:
        _dead_letter(redis_client, raw, job, "missing_callback_url")
        return

    try:
        ocr_result = _run_ocr_job(job)
    except APIError as exc:
        ocr_status, ocr_body = _serialize_api_error(exc)
        logger.warning(
            "ocr_queue_job_ocr_api_error job_id=%s session_id=%s status=%s code=%s",
            job_id,
            session_id,
            ocr_status,
            exc.code,
        )
        ocr_result = {
            "status": "error",
            "error": f"ocr_http_{ocr_status}",
            "raw_body": ocr_body[:1000],
        }
    except Exception as exc:
        logger.exception("ocr_queue_job_ocr_failed job_id=%s session_id=%s", job_id, session_id)
        ocr_result = {"status": "error", "error": "ocr_request_failed", "detail": str(exc)}

    callback_payload = {
        "job": job,
        "ocr_result": ocr_result,
        "source": "ocr_service_queue",
    }

    try:
        callback_status, callback_body = _post_json(
            OCR_CALLBACK_URL,
            callback_payload,
            timeout=OCR_CALLBACK_TIMEOUT_SECONDS,
            headers=_callback_headers(),
        )
        if 200 <= callback_status < 300:
            logger.info(
                "ocr_queue_job_callback_ok job_id=%s session_id=%s status=%s",
                job_id,
                session_id,
                callback_status,
            )
            _ack(redis_client, raw)
            return
        raise RuntimeError(f"callback_http_{callback_status}: {callback_body[:300]}")
    except Exception as exc:
        logger.exception("ocr_queue_job_callback_failed job_id=%s session_id=%s", job_id, session_id)
        if attempts < OCR_JOB_MAX_ATTEMPTS:
            _requeue(redis_client, raw, job)
            time.sleep(1.0)
            return
        _dead_letter(redis_client, raw, job, str(exc))


def _consume_loop(worker_index: int) -> None:
    redis_client = Redis.from_url(OCR_QUEUE_REDIS_URL, decode_responses=True)
    redis_available = True
    logger.info(
        "ocr_queue_consumer_thread_started worker=%s redis=%s queue=%s processing=%s callback_url=%s callback_timeout_seconds=%s",
        worker_index,
        OCR_QUEUE_REDIS_URL,
        OCR_QUEUE_NAME,
        OCR_PROCESSING_QUEUE_NAME,
        OCR_CALLBACK_URL or "<empty>",
        OCR_CALLBACK_TIMEOUT_SECONDS,
    )
    while True:
        try:
            raw = redis_client.brpoplpush(OCR_QUEUE_NAME, OCR_PROCESSING_QUEUE_NAME, timeout=OCR_QUEUE_BLOCK_SECONDS)
            if not redis_available:
                logger.info("ocr_queue_consumer_redis_reconnected redis=%s", OCR_QUEUE_REDIS_URL)
                redis_available = True
            if not raw:
                time.sleep(OCR_QUEUE_IDLE_SLEEP_SECONDS)
                continue
            _process_job(redis_client, raw)
        except RedisConnectionError as exc:
            if redis_available:
                logger.warning(
                    "ocr_queue_consumer_redis_unavailable worker=%s redis=%s error=%s",
                    worker_index,
                    OCR_QUEUE_REDIS_URL,
                    exc,
                )
                redis_available = False
            time.sleep(OCR_QUEUE_IDLE_SLEEP_SECONDS)
        except Exception:
            logger.exception("ocr_queue_consumer_loop_failed worker=%s", worker_index)
            time.sleep(OCR_QUEUE_IDLE_SLEEP_SECONDS)


def main() -> None:
    logger.info(
        "ocr_queue_consumer_started redis=%s queue=%s processing=%s processing_mode=%s worker_threads=%s callback_url=%s callback_timeout_seconds=%s",
        OCR_QUEUE_REDIS_URL,
        OCR_QUEUE_NAME,
        OCR_PROCESSING_QUEUE_NAME,
        "local_parallel",
        OCR_WORKER_THREADS,
        OCR_CALLBACK_URL or "<empty>",
        OCR_CALLBACK_TIMEOUT_SECONDS,
    )
    threads: list[threading.Thread] = []
    for worker_index in range(1, OCR_WORKER_THREADS + 1):
        thread = threading.Thread(
            target=_consume_loop,
            args=(worker_index,),
            name=f"ocr-queue-consumer-{worker_index}",
            daemon=False,
        )
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
