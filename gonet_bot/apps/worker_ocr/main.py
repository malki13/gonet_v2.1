"""Worker local que consume la cola OCR compartida y procesa comprobantes."""

import asyncio
import logging

from packages.agents.billing_async import BillingAsyncProcessor
from packages.integrations.ocr_queue import OCRJobQueue
from packages.shared.config import get_settings
from packages.shared.logging import setup_logging

setup_logging()
logger = logging.getLogger("worker_ocr")


async def worker_loop() -> None:
    """Consume trabajos OCR de la cola y entrega el resultado al callback."""
    settings = get_settings()
    queue = OCRJobQueue()
    processor = BillingAsyncProcessor()
    logger.info(
        "worker_ocr_started poll_seconds=%s queue=%s",
        settings.ocr_worker_poll_seconds,
        settings.ocr_queue_name,
    )
    while True:
        try:
            job = await queue.dequeue(timeout=settings.ocr_queue_block_seconds)
            if job is None:
                await asyncio.sleep(settings.ocr_worker_poll_seconds)
                continue
            logger.info("worker_ocr_job_received job_id=%s session_id=%s", job.job_id, job.session_id)
            result = await processor.process(job)
            logger.info(
                "worker_ocr_job_processed job_id=%s session_id=%s status=%s",
                job.job_id,
                job.session_id,
                result.get("status"),
            )
        except Exception:
            logger.exception("worker_ocr_loop_failed")
            await asyncio.sleep(settings.ocr_worker_poll_seconds)


def run() -> None:
    """Arranca el worker OCR en primer plano."""
    asyncio.run(worker_loop())


if __name__ == "__main__":
    run()
