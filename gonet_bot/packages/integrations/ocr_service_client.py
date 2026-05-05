"""Cliente HTTP para el servicio OCR externo."""

import httpx

from packages.shared.config import get_settings
from packages.shared.schemas import Attachment


class OCRServiceClient:
    """Cliente de Cliente HTTP del servicio OCR.."""
    def __init__(self) -> None:
        """Inicializa el ocrserviceclient con la configuracion necesaria."""
        self.settings = get_settings()

    @property
    def enabled(self) -> bool:
        """Indica si la integracion esta habilitada por configuracion."""
        return bool(self.settings.ocr_service_url or self.settings.mock_mode)

    async def analyze(self, attachment: Attachment, *, notify_gonet_bot: bool = False) -> dict | None:
        """Analiza la entrada y devuelve el resultado estructurado."""
        if self.settings.mock_mode and not self.settings.ocr_service_url:
            if isinstance(attachment, dict):
                filename = str(attachment.get("filename") or "").lower()
            else:
                filename = (attachment.filename or "").lower()
            if "retry" in filename or "borroso" in filename:
                return {
                    "status": "ok",
                    "estado": "retry",
                    "debe_reintentar": True,
                    "texto_extraido": "No se pudo leer correctamente el comprobante",
                    "raw": {"mock": True},
                }
            return {
                "status": "ok",
                "estado": "validated",
                "debe_reintentar": False,
                "texto_extraido": "Comprobante demo BANCO PICHINCHA código DEMO123 valor 18.50",
                "documento": "DEMO123",
                "valor": 18.50,
                "bank": "BANCO PICHINCHA",
                "raw": {"mock": True},
            }
        if not self.enabled:
            return None
        payload = {
            "notify_gonet_bot": notify_gonet_bot,
            "filename": attachment.filename,
            "mimetype": attachment.mime_type,
            "file_base64": attachment.base64_data,
            "url": attachment.url,
        }
        async with httpx.AsyncClient(timeout=max(self.settings.ocr_service_timeout_seconds, 60.0)) as client:
            response = await client.post(f"{self.settings.ocr_service_url.rstrip('/')}/v1/ocr", json=payload)
            response.raise_for_status()
            return response.json()
