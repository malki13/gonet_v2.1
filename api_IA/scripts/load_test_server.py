import os
import time
from typing import Any

from src.ocr_service.api import app_factory
from src.ocr_service.domain.models import Usage

SLEEP_SECONDS = max(0.0, float(os.getenv("LOAD_TEST_SLEEP_SECONDS", "0.20")))
CPU_BURN_MS = max(0, int(os.getenv("LOAD_TEST_CPU_BURN_MS", "0")))
RETURN_RETRY = os.getenv("LOAD_TEST_RETURN_RETRY", "false").strip().lower() in {"1", "true", "yes", "on"}


def _burn_cpu(milliseconds: int) -> None:
    if milliseconds <= 0:
        return
    deadline = time.perf_counter() + (milliseconds / 1000.0)
    value = 0
    while time.perf_counter() < deadline:
        value += 1
    if value < 0:
        raise RuntimeError("unreachable")


def _fake_process_document(_document: Any) -> dict[str, Any]:
    if SLEEP_SECONDS > 0:
        time.sleep(SLEEP_SECONDS)
    _burn_cpu(CPU_BURN_MS)
    return {
        "texto_extraido": "LOAD TEST OCR",
        "campos": {
            "numero_documento": "123456789",
            "entidad_bancaria": "BANCO PICHINCHA",
            "fecha": "2026/ene./19",
            "hora": "07:34",
            "total": "25.59",
        },
        "needs_retry": RETURN_RETRY,
        "retry_reasons": ["texto_insuficiente"] if RETURN_RETRY else [],
        "usage": Usage(input_tokens=0, output_tokens=0, total_tokens=0),
    }


def _fake_process_tesseract_image(_image_bytes: bytes) -> dict[str, Any]:
    if SLEEP_SECONDS > 0:
        time.sleep(SLEEP_SECONDS)
    _burn_cpu(CPU_BURN_MS)
    return {
        "texto_extraido": "LOAD TEST TESSERACT",
        "score": 200,
        "variant": "original",
        "variant_count": 1,
    }


app_factory.GONET_BOT_SEND_URL = ""
app_factory.ocr_service.process_document = _fake_process_document
app_factory.ocr_service.process_tesseract_image = _fake_process_tesseract_image

app = app_factory.create_app()
