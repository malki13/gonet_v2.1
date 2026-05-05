"""Callback seguro para resultados asíncronos del OCR."""

from fastapi import APIRouter, Header, HTTPException

from packages.agents.billing_async import BillingAsyncProcessor
from packages.integrations.ocr_callback_store import build_ocr_callback_store
from packages.shared.config import get_settings
from packages.shared.schemas import OCRCallbackPayload

router = APIRouter(prefix="/v1/ocr", tags=["ocr"])


@router.post("/callback")
async def handle_ocr_callback(
    payload: OCRCallbackPayload,
    x_ocr_callback_secret: str | None = Header(default=None, alias="X-OCR-Callback-Secret"),
) -> dict:
    """Maneja el callback OCR y avanza el flujo."""
    settings = get_settings()
    expected_secret = (settings.ocr_callback_secret or "").strip()
    if not expected_secret:
        raise HTTPException(status_code=503, detail="ocr_callback_secret_not_configured")
    if x_ocr_callback_secret != expected_secret:
        raise HTTPException(status_code=401, detail="invalid_ocr_callback_secret")

    callback_store = build_ocr_callback_store()
    claim_status, previous_result = await callback_store.start(payload.job.job_id)
    if claim_status == "completed":
        return {"status": "accepted", "deduplicated": True, "result": previous_result}
    if claim_status == "processing":
        return {
            "status": "accepted",
            "deduplicated": True,
            "result": {"status": "duplicate_in_progress", "job_id": payload.job.job_id},
        }

    processor = BillingAsyncProcessor()
    try:
        result = await processor.process_result(payload.job, payload.ocr_result, source=payload.source)
    except Exception:
        await callback_store.release(payload.job.job_id)
        raise
    await callback_store.complete(payload.job.job_id, result)
    return {"status": "accepted", "result": result}
