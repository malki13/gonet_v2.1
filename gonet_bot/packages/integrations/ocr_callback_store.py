"""Control de concurrencia y deduplicacion para callbacks OCR."""

import json
import logging
from dataclasses import dataclass
from functools import lru_cache

from packages.shared.config import get_settings
from packages.shared.errors import OCRCallbackStoreError

logger = logging.getLogger("ocr_callback_store")

try:
    import redis.asyncio as redis_asyncio
except ImportError:  # pragma: no cover
    redis_asyncio = None


@dataclass
class InMemoryOCRCallbackStore:
    """Almacena y recupera datos de las integraciones externas."""
    _processing: set[str]
    _results: dict[str, dict]

    async def start(self, job_id: str) -> tuple[str, dict | None]:
        """Inicia Almacena y recupera datos de las integraciones externas."""
        if job_id in self._results:
            return "completed", self._results[job_id]
        if job_id in self._processing:
            return "processing", None
        self._processing.add(job_id)
        return "acquired", None

    async def complete(self, job_id: str, result: dict) -> None:
        """Devuelve el complete."""
        self._processing.discard(job_id)
        self._results[job_id] = result

    async def release(self, job_id: str) -> None:
        """Devuelve el release."""
        self._processing.discard(job_id)


class RedisOCRCallbackStore:
    """Almacena y recupera datos de las integraciones externas."""
    def __init__(self, url: str, *, lock_ttl_seconds: int, result_ttl_seconds: int) -> None:
        """Inicializa el redisocrcallbackstore con la configuracion necesaria."""
        self._url = url
        self._lock_ttl_seconds = max(30, int(lock_ttl_seconds))
        self._result_ttl_seconds = max(self._lock_ttl_seconds, int(result_ttl_seconds))
        self._client = None

    def _lock_key(self, job_id: str) -> str:
        """Devuelve el clave candado."""
        return f"ocr:callback:lock:{job_id}"

    def _result_key(self, job_id: str) -> str:
        """Devuelve el clave resultado."""
        return f"ocr:callback:result:{job_id}"

    async def _client_or_raise(self):
        """Devuelve el raise client or."""
        if redis_asyncio is None:
            raise OCRCallbackStoreError("ocr_callback_store_requires_redis_asyncio")
        if self._client is not None:
            return self._client
        try:
            self._client = redis_asyncio.from_url(self._url, encoding="utf-8", decode_responses=True)
            await self._client.ping()
            return self._client
        except Exception as exc:
            self._client = None
            logger.exception("ocr_callback_store_unavailable redis=%s", self._url)
            raise OCRCallbackStoreError("ocr_callback_store_unavailable") from exc

    async def _run(self, operation: str, action):
        """Arranca el flujo principal con la configuracion cargada."""
        try:
            return await action
        except OCRCallbackStoreError:
            raise
        except Exception as exc:
            self._client = None
            logger.exception("ocr_callback_store_operation_failed operation=%s", operation)
            raise OCRCallbackStoreError("ocr_callback_store_unavailable") from exc

    async def start(self, job_id: str) -> tuple[str, dict | None]:
        """Inicia Almacena y recupera datos de las integraciones externas."""
        client = await self._client_or_raise()
        raw_result = await self._run("start_get_result", client.get(self._result_key(job_id)))
        if raw_result:
            return "completed", json.loads(raw_result)
        acquired = await self._run(
            "start_acquire_lock",
            client.set(self._lock_key(job_id), "processing", ex=self._lock_ttl_seconds, nx=True),
        )
        if acquired:
            return "acquired", None
        raw_result = await self._run("start_get_result_after_lock", client.get(self._result_key(job_id)))
        if raw_result:
            return "completed", json.loads(raw_result)
        return "processing", None

    async def complete(self, job_id: str, result: dict) -> None:
        """Devuelve el complete."""
        client = await self._client_or_raise()
        payload = json.dumps(result, ensure_ascii=False)
        pipeline = client.pipeline()
        pipeline.set(self._result_key(job_id), payload, ex=self._result_ttl_seconds)
        pipeline.delete(self._lock_key(job_id))
        await self._run("complete_pipeline", pipeline.execute())

    async def release(self, job_id: str) -> None:
        """Devuelve el release."""
        client = await self._client_or_raise()
        await self._run("release_lock", client.delete(self._lock_key(job_id)))


@lru_cache(maxsize=1)
def build_ocr_callback_store() -> RedisOCRCallbackStore | InMemoryOCRCallbackStore:
    """Construye store ocr callback a partir del contexto disponible."""
    settings = get_settings()
    if settings.redis_url:
        return RedisOCRCallbackStore(
            settings.redis_url,
            lock_ttl_seconds=settings.ocr_callback_lock_ttl_seconds,
            result_ttl_seconds=settings.ocr_callback_result_ttl_seconds,
        )
    return InMemoryOCRCallbackStore(set(), {})
