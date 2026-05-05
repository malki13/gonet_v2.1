"""Adaptador de salida para WhatsApp."""

def normalize_whatsapp_recipient(recipient: str) -> str:
    """Normaliza recipient whatsapp."""
    return "".join(ch for ch in recipient if ch.isdigit() or ch == "+")

