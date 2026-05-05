"""Umbrales simples de confianza para decidir si hace falta aclaración."""

def needs_clarification(confidence: float) -> bool:
    """Indica si clarification se cumple."""
    return confidence < 0.55


def confident_enough(confidence: float) -> bool:
    """Devuelve el enough confident."""
    return confidence >= 0.80

