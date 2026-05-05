import base64
import importlib
import json
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from src.ocr_service.domain.models import APIError
from src.ocr_service.workers import queue_consumer


def _reload_queue_consumer(monkeypatch, **env):
    for name in (
        "OCR_QUEUE_REDIS_URL",
        "REDIS_URL",
        "OCR_CALLBACK_TIMEOUT_SECONDS",
        "OCR_HTTP_TIMEOUT_SECONDS",
        "OCR_WORKER_THREADS",
        "OCR_MAX_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    return importlib.reload(queue_consumer)


def test_queue_consumer_defaults_to_internal_redis(monkeypatch):
    module = _reload_queue_consumer(monkeypatch)

    assert module.OCR_QUEUE_REDIS_URL == "redis://redis:6379/0"


def test_queue_consumer_prefers_explicit_queue_redis_url(monkeypatch):
    module = _reload_queue_consumer(
        monkeypatch,
        OCR_QUEUE_REDIS_URL="redis://queue-redis:6379/2",
        REDIS_URL="redis://fallback-redis:6379/5",
    )

    assert module.OCR_QUEUE_REDIS_URL == "redis://queue-redis:6379/2"


def test_queue_consumer_falls_back_to_redis_url(monkeypatch):
    module = _reload_queue_consumer(monkeypatch, REDIS_URL="redis://shared-redis:6379/7")

    assert module.OCR_QUEUE_REDIS_URL == "redis://shared-redis:6379/7"


def test_queue_consumer_uses_callback_timeout_default(monkeypatch):
    module = _reload_queue_consumer(monkeypatch)

    assert module.OCR_CALLBACK_TIMEOUT_SECONDS == 30.0


def test_queue_consumer_uses_worker_threads_default(monkeypatch):
    module = _reload_queue_consumer(monkeypatch)

    assert module.OCR_WORKER_THREADS == 4


def test_queue_consumer_worker_threads_follow_ocr_max_concurrency(monkeypatch):
    module = _reload_queue_consumer(monkeypatch, OCR_MAX_CONCURRENCY="6")

    assert module.OCR_WORKER_THREADS == 6


def test_queue_consumer_prefers_explicit_worker_threads(monkeypatch):
    module = _reload_queue_consumer(monkeypatch, OCR_MAX_CONCURRENCY="6", OCR_WORKER_THREADS="3")

    assert module.OCR_WORKER_THREADS == 3


def test_queue_consumer_builds_document_from_attachment_base64(monkeypatch):
    module = _reload_queue_consumer(monkeypatch)

    document = module._build_job_document(
        {
            "attachments": [
                {
                    "filename": "ticket.png",
                    "mime_type": "image/png",
                    "base64_data": base64.b64encode(b"image-bytes").decode("ascii"),
                }
            ]
        }
    )

    assert document.filename == "ticket.png"
    assert document.mimetype == "image/png"
    assert document.content == b"image-bytes"
    assert document.is_pdf is False


class _RedisStub:
    def __init__(self) -> None:
        self.lrem_calls = []
        self.lpush_calls = []

    def lrem(self, queue_name, count, raw):
        self.lrem_calls.append((queue_name, count, raw))

    def lpush(self, queue_name, raw):
        self.lpush_calls.append((queue_name, raw))


def test_queue_consumer_processes_job_locally_and_callbacks(monkeypatch):
    module = _reload_queue_consumer(monkeypatch, OCR_CALLBACK_URL="http://callback.test")
    redis_client = _RedisStub()
    captured = {}

    monkeypatch.setattr(
        module,
        "_run_ocr_job",
        lambda _job: {
            "estado": "ok",
            "texto_extraido": "TEXTO OCR",
            "debe_reintentar": False,
            "motivos_reintento": [],
            "instrucciones_reintento": [],
            "uso": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "costo_estimado": {"input_cost": 0.0, "output_cost": 0.0, "total_cost": 0.0},
        },
    )

    def _fake_post(url, payload, *, timeout, headers=None):
        captured["url"] = url
        captured["payload"] = payload
        captured["timeout"] = timeout
        captured["headers"] = headers
        return 200, "ok"

    monkeypatch.setattr(module, "_post_json", _fake_post)

    raw = json.dumps(
        {
            "job_id": "job-1",
            "session_id": "session-1",
            "attachments": [
                {
                    "filename": "ticket.jpg",
                    "mime_type": "image/jpeg",
                    "base64_data": base64.b64encode(b"image-bytes").decode("ascii"),
                }
            ],
        }
    )

    module._process_job(redis_client, raw)

    assert captured["url"] == "http://callback.test"
    assert captured["payload"]["source"] == "ocr_service_queue"
    assert captured["payload"]["ocr_result"]["estado"] == "ok"
    assert captured["payload"]["job"]["worker_attempts"] == 1
    assert redis_client.lrem_calls == [(module.OCR_PROCESSING_QUEUE_NAME, 1, raw)]
    assert redis_client.lpush_calls == []


def test_queue_consumer_wraps_api_error_for_callback(monkeypatch):
    module = _reload_queue_consumer(monkeypatch, OCR_CALLBACK_URL="http://callback.test")
    redis_client = _RedisStub()
    captured = {}

    def _raise_api_error(_job):
        raise APIError("No file provided", status_code=400, code="missing_file")

    def _fake_post(url, payload, *, timeout, headers=None):
        captured["url"] = url
        captured["payload"] = payload
        return 200, "ok"

    monkeypatch.setattr(module, "_run_ocr_job", _raise_api_error)
    monkeypatch.setattr(module, "_post_json", _fake_post)

    raw = json.dumps({"job_id": "job-2", "session_id": "session-2", "attachments": []})

    module._process_job(redis_client, raw)

    assert captured["url"] == "http://callback.test"
    assert captured["payload"]["ocr_result"]["status"] == "error"
    assert captured["payload"]["ocr_result"]["error"] == "ocr_http_400"
    error_body = json.loads(captured["payload"]["ocr_result"]["raw_body"])
    assert error_body["code"] == "missing_file"
    assert redis_client.lrem_calls == [(module.OCR_PROCESSING_QUEUE_NAME, 1, raw)]
