import asyncio

import pytest

from packages.integrations.ocr_queue import OCRJobQueue
from packages.integrations.redis_store import RedisSessionStore
from packages.shared.config import get_settings
from packages.shared.errors import OCRQueueUnavailableError, SessionStoreError
from packages.shared.schemas import OCRJob


class _BrokenRedisModule:
    @staticmethod
    def from_url(*args, **kwargs):
        raise RuntimeError("boom")


def test_redis_session_store_fails_closed_when_redis_is_unavailable(monkeypatch):
    from packages.integrations import redis_store as redis_store_module

    monkeypatch.setattr(redis_store_module, "redis_asyncio", _BrokenRedisModule)
    store = RedisSessionStore("redis://broken:6379/0", ttl_seconds=60)

    with pytest.raises(SessionStoreError, match="shared_session_store_unavailable"):
        asyncio.run(store.get("session-1"))


def test_ocr_job_queue_requires_shared_redis(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "redis_url", None)
    queue = OCRJobQueue()
    job = OCRJob(job_id="job-1", session_id="session-1", recipient="593999")

    with pytest.raises(OCRQueueUnavailableError, match="shared_ocr_queue_requires_redis"):
        asyncio.run(queue.enqueue(job))


def test_ocr_job_queue_fails_closed_when_redis_is_unavailable(monkeypatch):
    from packages.integrations import ocr_queue as ocr_queue_module

    settings = get_settings()
    monkeypatch.setattr(settings, "redis_url", "redis://broken:6379/0")
    monkeypatch.setattr(ocr_queue_module, "redis_asyncio", _BrokenRedisModule)
    queue = OCRJobQueue()
    job = OCRJob(job_id="job-2", session_id="session-2", recipient="593999")

    with pytest.raises(OCRQueueUnavailableError, match="shared_ocr_queue_unavailable"):
        asyncio.run(queue.enqueue(job))
