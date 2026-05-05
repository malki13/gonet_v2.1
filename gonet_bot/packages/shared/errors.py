"""Excepciones compartidas para errores operativos y fallos de integración."""

class RoutingError(Exception):
    """Fallo de ruteo conversacional."""


class SharedStateUnavailableError(RuntimeError):
    """Infraestructura compartida no disponible para completar la operación."""


class SessionStoreError(SharedStateUnavailableError):
    """Fallo del store de sesión."""


class OCRQueueUnavailableError(SharedStateUnavailableError):
    """La cola OCR compartida no está disponible."""


class OCRCallbackStoreError(SharedStateUnavailableError):
    """El store de idempotencia del callback OCR no está disponible."""
