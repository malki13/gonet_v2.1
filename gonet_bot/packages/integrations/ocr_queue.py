"""Cola compartida para trabajos OCR asíncronos."""

import logging

from packages.shared.config import get_settings
from packages.shared.errors import OCRQueueUnavailableError
from packages.shared.schemas import OCRJob

logger = logging.getLogger("ocr_queue")

try:
    import redis.asyncio as redis_asyncio
except ImportError:  # pragma: no cover
    redis_asyncio = None


class OCRJobQueue:
    """Cola compartida para trabajos OCR asíncronos."""

    def __init__(self) -> None:
        """Inicializa el ocrjobqueue con la configuracion necesaria."""
        self.settings = get_settings()
        self._client = None

    async def _client_or_raise(self):
        """Devuelve el raise client or."""
        if redis_asyncio is None or not self.settings.redis_url:
            raise OCRQueueUnavailableError("shared_ocr_queue_requires_redis")
        if self._client is not None:
            return self._client
        try:
            self._client = redis_asyncio.from_url(self.settings.redis_url, encoding="utf-8", decode_responses=True)
            await self._client.ping()
            return self._client
        except Exception as exc:
            logger.exception("ocr_queue_redis_unavailable redis=%s", self.settings.redis_url)
            self._client = None
            raise OCRQueueUnavailableError("shared_ocr_queue_unavailable") from exc

    async def _run(self, operation: str, action):
        """Arranca el flujo principal con la configuracion cargada."""
        try:
            return await action
        except OCRQueueUnavailableError:
            raise
        except Exception as exc:
            self._client = None
            logger.exception("ocr_queue_operation_failed operation=%s", operation)
            raise OCRQueueUnavailableError("shared_ocr_queue_unavailable") from exc

    async def enqueue(self, job: OCRJob) -> dict:
        """Encola un trabajo OCR al inicio de la lista en Redis."""
        payload = job.model_dump_json()
        client = await self._client_or_raise()
        size = await self._run("enqueue", client.lpush(self.settings.ocr_queue_name, payload))
        return {"ok": True, "backend": "redis", "size": int(size)}

    async def dequeue(self, *, timeout: int | None = None) -> OCRJob | None:
        """Bloquea hasta extraer un trabajo OCR o retorna `None` por timeout."""
        block_seconds = max(1, timeout or self.settings.ocr_queue_block_seconds)
        client = await self._client_or_raise()
        item = await self._run("dequeue", client.brpop(self.settings.ocr_queue_name, timeout=block_seconds))
        if not item:
            return None
        _, raw = item
        if not raw:
            return None
        return OCRJob.model_validate_json(raw)

    async def size(self) -> int:
        """Retorna la cantidad de jobs pendientes en la cola."""
        client = await self._client_or_raise()
        return int(await self._run("size", client.llen(self.settings.ocr_queue_name)))
