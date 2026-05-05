import os

from src.ocr_service.api.app_factory import app, create_app
from src.ocr_service.services.ocr_core import DATASET_DEFAULT_DIR, OPENAI_OCR_DETAIL, OPENAI_OCR_MODEL, logger

__all__ = ["app", "create_app"]


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("QUART_DEBUG", os.getenv("FLASK_DEBUG", "false")).lower() == "true"

    logger.info("Starting OCR service")
    logger.info("model=%s detail=%s", OPENAI_OCR_MODEL, OPENAI_OCR_DETAIL)
    logger.info("dataset_default_dir=%s", DATASET_DEFAULT_DIR)

    app.run(host="0.0.0.0", port=port, debug=debug)
