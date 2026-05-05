"""Utilidades de texto y localización para el flujo de ventas."""

import re
from urllib.parse import unquote, urlparse

from packages.shared.sales_intents import (
    declines_commercial_registration,
    detect_commercial_catalog_segment,
    is_generic_sales_opening,
    is_sales_greeting,
    looks_like_commercial_followup,
    looks_like_plan_request,
)

NO_LOCATION_TEXTS = {
    "no",
    "no se",
    "no sé",
    "desconozco",
    "no aplica",
    "sin ubicación",
}
ECUADOR_PROVINCES = [
    "azuay",
    "bolivar",
    "cañar",
    "carchi",
    "chimborazo",
    "cotopaxi",
    "el oro",
    "esmeraldas",
    "galápagos",
    "guayas",
    "imbabura",
    "loja",
    "los ríos",
    "manabí",
    "manabi",
    "morona santiago",
    "napo",
    "orellana",
    "pastaza",
    "pichincha",
    "santa elena",
    "santo domingo",
    "sucumbíos",
    "sucumbios",
    "tungurahua",
    "zamora chinchipe",
]


def norm(value: str | None) -> str:
    """Devuelve el norm."""
    return re.sub(r"\s+", " ", (value or "").strip())


def contains_any(text: str, needles: list[str]) -> bool:
    """Devuelve el any contains."""
    lowered = (text or "").lower().strip()
    return any(needle.lower() in lowered for needle in needles)


def _clean_location(text: str) -> str:
    """Limpia location."""
    lowered = (text or "").lower().strip()
    prefixes = [
        "en la provincia de ",
        "en la provincia del ",
        "en la provincia ",
        "provincia de ",
        "provincia del ",
        "en la ciudad de ",
        "en la ciudad del ",
        "en la ciudad ",
        "ciudad de ",
        "ciudad del ",
        "mi ciudad es ",
        "mi sector es ",
        "mi zona es ",
        "sector ",
        "zona ",
        "estoy en ",
        "me encuentro en ",
        "en ",
    ]
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return lowered[len(prefix) :].strip()
    return lowered


def _is_greeting(text: str) -> bool:
    """Indica si greeting se cumple."""
    return is_sales_greeting(text)


def _looks_like_plan_request(text: str) -> bool:
    """Devuelve el request looks like plan."""
    return looks_like_plan_request(text)


def _detect_commercial_catalog_segment(text: str) -> str | None:
    """Detecta commercial catalog segment."""
    return detect_commercial_catalog_segment(text)


def _declines_commercial_registration(text: str) -> bool:
    """Devuelve el registration declines commercial."""
    return declines_commercial_registration(text)


def _looks_like_commercial_followup(text: str) -> bool:
    """Devuelve el followup looks like commercial."""
    return looks_like_commercial_followup(text)


def _is_generic_opening_message(text: str) -> bool:
    """Indica si mensaje generic opening se cumple."""
    return is_generic_sales_opening(text)


def _looks_like_payment_request(text: str) -> bool:
    """Devuelve el request looks like payment."""
    lowered = (text or "").lower().strip()
    if not lowered:
        return False
    return contains_any(
        lowered,
        [
            "facturacion",
            "facturación",
            "factura",
            "facturas",
            "pago",
            "pagos",
            "pagare",
            "pagar",
            "comprobante",
            "comprobantes",
            "transferencia",
            "transferencias",
            "deposito",
            "depósito",
            "depositos",
            "depósitos",
            "metodos de pago",
            "métodos de pago",
            "formas de pago",
            "pago mensual",
            "fecha de pago",
            "dia de pago",
            "día de pago",
            "corte",
            "reclamo",
            "reclamos",
            "soy cliente",
        ],
    )


def _strip_agency_words(text: str) -> str:
    """Devuelve el words strip agency."""
    cleaned = norm(text)
    lowered = cleaned.lower()
    province = next((item for item in ECUADOR_PROVINCES if item in lowered), None)
    if province:
        return province
    replacements = [
        r"\b(agencia|agencias|sucursal|sucursales|oficina|oficinas)\b",
        r"\b(no|mejor|quiero|quisiera|deseo|necesito|deme|dame|mostrar|muestre|busco|saber|informacion|información|y|otra|otro|tambien|también|ir|voy|visitar|ubicar)\b",
        r"\b(donde|dónde|queda|quedan|ubicar|ubique)\b",
        r"\b(las|los|la|el|una|un)\b",
        r"\b(de la|de los|de las|del|de|en la|en el|en|para|a|al)\b",
    ]
    for pattern in replacements:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    if cleaned in {"", "ir", "ir a", "a", "al"}:
        return ""
    return cleaned


def _strip_urls(text: str) -> str:
    """Devuelve el urls strip."""
    return re.sub(r"https?://\S+", "", text or "", flags=re.IGNORECASE).strip()


def _street_candidate(text: str) -> str:
    """Devuelve el candidate street."""
    cleaned = re.sub(r"\s+", " ", _strip_urls(text or "")).strip()
    if cleaned.lower() in {"ubicacion compartida", "ubicación compartida"}:
        return ""
    return cleaned


def _looks_like_precise_address(text: str) -> bool:
    """Devuelve el address looks like precise."""
    cleaned = _street_candidate(text).lower()
    if not cleaned:
        return False
    hints = (
        "av ",
        "av.",
        "avenida",
        "calle",
        "cdla",
        "ciudadela",
        "urbanizacion",
        "urbanización",
        "barrio",
        "sector",
        "mz",
        "manzana",
        "solar",
        "km",
        "via ",
        "vía ",
        "pasaje",
        "pje",
        "diag",
        "diagonal",
        "casa",
        "bloque",
        "edificio",
    )
    if any(hint in cleaned for hint in hints):
        return True
    if " y " in cleaned and len(cleaned.split()) >= 4:
        return True
    return bool(re.search(r"\b\d+[a-z]?\b", cleaned) and len(cleaned.split()) >= 3)


def _title_case(value: str | None) -> str | None:
    """Devuelve el case title."""
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip().title()


def _extract_coordinates_from_text(text: str) -> dict:
    """Extrae coordinates from text."""
    decoded = unquote(text or "")
    patterns = [
        r"/maps/search/(-?\d{1,2}\.\d+),\+?(-?\d{1,3}\.\d+)",
        r"@(-?\d{1,2}\.\d+),\+?(-?\d{1,3}\.\d+)",
        r"[?&](?:q|query|ll)=(-?\d{1,2}\.\d+),\+?(-?\d{1,3}\.\d+)",
        r"!3d(-?\d{1,2}\.\d+)!4d(-?\d{1,3}\.\d+)",
        r"(?<![\d.])(-?\d{1,2}\.\d+)\s*,\s*\+?(-?\d{1,3}\.\d+)(?![\d.])",
    ]
    for pattern in patterns:
        match = re.search(pattern, decoded)
        if match:
            return {"latitude": float(match.group(1)), "longitude": float(match.group(2))}
    return {}


def _extract_urls(text: str) -> list[str]:
    """Extrae urls."""
    return re.findall(r"https?://\S+", text or "", flags=re.IGNORECASE)


def _is_google_maps_short_link(url: str) -> bool:
    """Indica si link google maps short se cumple."""
    parsed = urlparse(url or "")
    host = parsed.netloc.lower()
    path = parsed.path.lower().strip("/")
    return host == "maps.app.goo.gl" or (host == "goo.gl" and path.startswith("maps"))


def _extract_location_from_text(text: str) -> dict:
    """Extrae location from text."""
    cleaned = _clean_location(text)
    if not cleaned or cleaned in NO_LOCATION_TEXTS:
        return {}
    if any(token in cleaned for token in ("http://", "https://", "maps.app.goo.gl", "goo.gl/maps")):
        without_urls = _strip_urls(cleaned)
        return {"address": without_urls} if without_urls else {}

    explicit = re.search(r"(ciudad|provincia|zona|sector)\s*[:=-]?\s*(.+)", cleaned, flags=re.IGNORECASE)
    if explicit:
        label = explicit.group(1).lower()
        value = explicit.group(2).strip()
        if label in {"zona", "sector"}:
            return {"zone": value}
        if label == "provincia":
            return {"province": value}
        return {"city": value}

    parts = [part.strip() for part in re.split(r"[,;/|-]", cleaned) if part.strip()]
    if len(parts) >= 2:
        return {"city": parts[0], "zone": parts[1], "address": cleaned}

    province = next((item for item in ECUADOR_PROVINCES if item in cleaned), None)
    if province and ("provincia" in cleaned or len(cleaned.split()) <= 3):
        return {"province": province}

    if contains_any(
        cleaned,
        [
            "plan",
            "planes",
            "agencia",
            "agencias",
            "informacion",
            "información",
            "facturacion",
            "facturación",
            "servicio",
            "servicios",
            "quiero",
            "saber",
            "donde",
            "dónde",
            "quedan",
            "queda",
            "hola",
            "buenas",
            "asesor",
        ],
    ):
        return {}
    if len(cleaned.split()) <= 6:
        return {"city": cleaned}
    return {"address": cleaned}
