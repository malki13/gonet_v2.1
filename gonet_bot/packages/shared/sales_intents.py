"""Detección de intención comercial y señales de seguimiento."""

from __future__ import annotations

import re
from dataclasses import dataclass

from packages.shared.utils import contains_any_phrase, matches_any_phrase, normalize_text, token_matches

SALES_GREETING_KEYWORDS = {
    "hola",
    "buenas",
    "buenos dias",
    "buenas tardes",
    "buenas noches",
    "buen dia",
    "hello",
    "hi",
    "holi",
}
SALES_DISCOVERY_PHRASES = (
    "q vendes",
    "q venden",
    "q ofreces",
    "q ofrecen",
    "q tienes",
    "q tienen",
    "que vendes",
    "que venden",
    "que ofreces",
    "que ofrecen",
    "que tienes",
    "que tienen",
    "qué vendes",
    "qué venden",
    "qué ofreces",
    "qué ofrecen",
    "qué tienes",
    "qué tienen",
)
SALES_DISCOVERY_VERBS = (
    "vendes",
    "venden",
    "ofreces",
    "ofrecen",
    "tienes",
    "tienen",
)
SALES_DISCOVERY_CATALOG_FALLBACK_TERMS = (
    "internet",
    "servicio",
    "servicios",
)
SALES_MENU_TERMS = ("menu", "menú", "opciones")
SALES_AGENCY_TERMS = ("agencia", "agencias", "sucursal", "oficina", "donde quedan", "dónde quedan")
SALES_INFO_TERMS = (
    "informacion comercial",
    "información comercial",
    "quiero informacion",
    "quiero información",
    "cobertura",
    "disponibilidad",
    "quiero que me contacten",
    "contratar",
    "me interesa",
    "quiero internet",
    "publicacion",
    "anuncio",
    "anuncios",
    "publicidad",
)
SALES_INSTALLATION_TIME_TERMS = (
    "tiempo de instalacion",
    "tiempo de instalación",
    "demora la instalación",
    "demora la instalacion",
)
SALES_INSTALLATION_COST_TERMS = (
    "costo de instalacion",
    "costo de instalación",
    "cuánto cuesta la instalación",
    "cuanto cuesta la instalacion",
)
SALES_REQUIREMENTS_TERMS = ("requisitos", "qué necesito", "que necesito")
SALES_PAYMENT_METHOD_TERMS = ("metodos de pago", "métodos de pago", "formas de pago")
SALES_MONTHLY_PAYMENT_TERMS = ("pago mensual", "fecha de pago", "día de pago", "dia de pago", "corte")
SALES_CONTRACT_TERMS = ("contrato", "12 meses", "24 meses", "36 meses")
SALES_CONTACT_TERMS = ("contact center", "teléfono", "telefono", "número de contacto")
SALES_WARRANTY_TERMS = ("garantia extendida", "garantía extendida")
SALES_GENERIC_OPENING_TERMS = (
    "informacion",
    "información",
    "necesito informacion",
    "necesito información",
    "necesito ayuda",
    "quiero ayuda",
    "ayuda",
    "asesoria",
    "asesoría",
    "quiero consultar",
    "deseo informacion",
    "deseo información",
)
SALES_GENERIC_OPENING_BLOCKERS = (
    "facturacion",
    "facturación",
    "factura",
    "pago",
    "plan",
    "planes",
    "promo",
    "promos",
    "promocion",
    "promoción",
    "promociones",
    "cobertura",
    "agencia",
    "agencias",
    "sucursal",
    "oficina",
    "internet",
    "contratar",
)
SALES_REGISTRATION_DECLINE_TERMS = (
    "solo quiero ver los planes",
    "solo quiero ver planes",
    "solo quiero informacion",
    "solo quiero información",
    "solo quiero saber los planes",
    "solo muestrame los planes",
    "solo muéstrame los planes",
    "solo dame los planes",
    "solo deme los planes",
    "sin registrar mis datos",
    "sin compartir mis datos",
    "no quiero registrar mis datos",
    "no quiero compartir mis datos",
    "no deseo compartir mis datos",
    "prefiero no compartir mis datos",
)
SALES_CAPTURE_TERMS = (
    "contratar",
    "quiero que me contacten",
    "quiero que un asesor me contacte",
    "quiero que un asesor me llame",
    "me contacten",
    "me llame un asesor",
    "quiero instalar",
    "quiero adquirir",
    "quiero solicitar",
    "deseo contratar",
)
SALES_RECOMMENDED_PLAN_ACCEPT_TERMS = (
    "ese me sirve",
    "este me sirve",
    "ese sirve",
    "este sirve",
    "me sirve",
    "me sirve ese",
    "si me sirve",
    "sí me sirve",
    "ese quiero",
    "este quiero",
    "quiero ese",
    "quiero este",
    "quiero ese plan",
    "me quedo con ese",
    "me quedo con este",
    "me interesa ese",
    "ese esta bien",
    "ese está bien",
    "esta bien ese",
    "está bien ese",
    "ese mismo",
    "dale con ese",
    "de una con ese",
    "avancemos con ese",
    "avancemos con este",
    "vamos con ese",
    "vamos con este",
)
SALES_RECOMMENDED_PLAN_ACCEPT_EXACT = {
    "ese",
    "este",
    "si",
    "sí",
    "dale",
    "de una",
    "listo",
    "perfecto",
    "avancemos",
}
SALES_RECOMMENDED_PLAN_REJECT_TERMS = (
    "no me sirve",
    "ese no me sirve",
    "no quiero ese",
    "no ese",
    "otro plan",
    "otra opcion",
    "otra opción",
)
SALES_FULL_CATALOG_TERMS = (
    "todos",
    "todas",
    "ver todos",
    "todos los planes",
    "todas las opciones",
    "catalogo completo",
    "catálogo completo",
    "quiero planes",
    "quiero los planes",
    "quiero ver planes",
    "quiero ver los planes",
    "dame planes",
    "dame los planes",
    "muestrame planes",
    "muéstrame planes",
    "muestrame los planes",
    "muéstrame los planes",
    "pasame planes",
    "pásame planes",
    "pasame los planes",
    "pásame los planes",
    "solo quiero los planes",
    "solo quiero planes",
    "solo muestrame los planes",
    "solo muéstrame los planes",
    "solo dame los planes",
    "solo deme los planes",
    "muéstrame todos",
    "muestrame todos",
    "muestrame los demas",
    "muéstrame los demás",
    "ver los demas",
    "ver los demás",
    "pasame los demas",
    "pásame los demás",
)
SALES_RECOMMENDATION_CHOICE_TERMS = (
    "recomendacion",
    "recomendación",
    "personalizada",
    "personalizado",
    "recomendacion personalizada",
    "recomendación personalizada",
    "quiero una recomendacion",
    "quiero una recomendación",
    "dame una recomendacion",
    "dame una recomendación",
    "recomiendame un plan",
    "recomiéndame un plan",
    "recomiendame uno",
    "recomiéndame uno",
    "cual me recomiendas",
    "cuál me recomiendas",
    "ayudame a elegir",
    "ayúdame a elegir",
)
AGENCY_FOLLOWUP_PROVINCES = (
    "azuay",
    "bolivar",
    "cañar",
    "carchi",
    "chimborazo",
    "cotopaxi",
    "el oro",
    "esmeraldas",
    "galapagos",
    "galápagos",
    "guayas",
    "imbabura",
    "loja",
    "los rios",
    "los ríos",
    "manabi",
    "manabí",
    "morona santiago",
    "napo",
    "orellana",
    "pastaza",
    "pichincha",
    "santa elena",
    "santo domingo",
    "sucumbios",
    "sucumbíos",
    "tungurahua",
    "zamora chinchipe",
)
ROUTABLE_SALES_INTENTS = frozenset(
    {
        "agencies",
        "info",
        "instalacion_tiempo",
        "instalacion_costo",
        "requisitos",
        "contrato",
        "contacto",
        "garantia",
    }
)
SALES_CATALOG_TERMS = (
    "plan",
    "planes",
    "combo",
    "combos",
    "promo",
    "promos",
    "promocion",
    "promoción",
    "promociones",
    "internet",
    "tv",
    "television",
    "televisión",
    "gonectados",
    "residencial",
    "pyme",
    "pymes",
    "servicio",
    "servicios",
)
EXPLICIT_SALES_CATALOG_TERMS = (
    "plan",
    "planes",
    "combo",
    "combos",
    "promo",
    "promos",
    "promocion",
    "promoción",
    "promociones",
    "tv",
    "television",
    "televisión",
    "gonectados",
    "residencial",
    "pyme",
    "pymes",
)
COMMERCIAL_FOLLOWUP_TERMS = (
    "pyme",
    "pymes",
    "hogar",
    "casa",
    "departamento",
    "domicilio",
    "residencial",
    "empresa",
    "empresas",
    "negocio",
    "negocios",
    "corporativo",
    "corporativa",
    "corporativos",
    "corporativas",
    "precio",
    "precios",
    "velocidad",
    "velocidades",
    "promo",
    "promos",
    "promocion",
    "promoción",
    "recomiendas",
    "recomienda",
    "conviene",
    "detalle",
    "detalles",
    "cual",
    "cuál",
)
COMMERCIAL_FOLLOWUP_PATTERN = re.compile(
    (
        r"\b("
        r"pyme|pymes|hogar|casa|departamento|domicilio|residencial|empresa|empresas|negocio|negocios|"
        r"corporativo|corporativa|corporativos|corporativas|"
        r"precio|precios|velocidad|velocidades|promo|promos|promocion|promoción|"
        r"recomiendas|recomienda|conviene|detalle|detalles|cual|cuál"
        r")\b"
    )
)
AGENCY_FOLLOWUP_PATTERN = re.compile(
    r"^(y|en|de|del|otra|otro|tambien|también)\s+[a-záéíóúñ]+(?:\s+[a-záéíóúñ]+){0,2}$"
)
AGENCY_FOLLOWUP_SECONDARY_PATTERN = re.compile(
    r"^(y|otra|otro|tambien|también)\s+(en|de|del)\s+[a-záéíóúñ]+(?:\s+[a-záéíóúñ]+){0,2}$"
)
COMMERCIAL_BANDWIDTH_PATTERN = re.compile(r"\b\d{2,4}\s*(megas?|mbps?)\b")
CHEAPNESS_PATTERN = re.compile(r"\bm[aá]s\s+barat[oa]\b")
LOW_PRICE_PATTERN = re.compile(r"\bbarat[oa]\b")
ECONOMIC_PATTERN = re.compile(r"\becon[oó]mic[oa]\b")


@dataclass(frozen=True, slots=True)
class SalesMessageAnalysis:
    """Resultado del análisis de intención comercial y seguimiento."""
    normalized: str
    intent: str
    catalog_segment: str | None
    is_greeting: bool
    is_discovery_query: bool
    is_generic_opening: bool
    is_commercial_followup: bool
    declines_registration: bool
    is_agency_followup: bool
    has_explicit_catalog_terms: bool
    wants_capture: bool
    wants_full_catalog: bool
    wants_personalized_recommendation: bool
    accepts_recommended_plan: bool

    @property
    def routes_to_sales_classifier(self) -> bool:
        """Devuelve el classifier routes to sales."""
        return (
            self.is_discovery_query
            or self.is_commercial_followup
            or self.intent in ROUTABLE_SALES_INTENTS
            or (self.intent == "plan" and self.has_explicit_catalog_terms)
        )


def _contains_any(text: str, needles: tuple[str, ...] | list[str]) -> bool:
    """Devuelve el any contains."""
    return contains_any_phrase(text, needles)


def _clean_punctuation(text: str) -> str:
    """Limpia punctuation."""
    return re.sub(r"[?¿!¡.,;:]", "", text or "").strip()


def _token_matches_any(token: str, options: tuple[str, ...] | list[str] | set[str]) -> bool:
    """Devuelve el any token matches."""
    return any(token_matches(token, option) for option in options)


def _is_greeting_normalized(normalized: str) -> bool:
    """Indica si normalized greeting se cumple."""
    cleaned = re.sub(r"[!¡?¿.,]", "", normalized).strip()
    return matches_any_phrase(cleaned, SALES_GREETING_KEYWORDS)


def _is_discovery_query_normalized(normalized: str) -> bool:
    """Indica si normalized discovery query se cumple."""
    if _contains_any(normalized, SALES_DISCOVERY_PHRASES):
        return True
    if not normalized:
        return False
    cleaned = _clean_punctuation(normalized)
    tokens = cleaned.split()
    question_index = next((idx for idx, token in enumerate(tokens[:4]) if token_matches(token, "que")), None)
    if (
        question_index is not None
        and any(_token_matches_any(token, SALES_DISCOVERY_CATALOG_FALLBACK_TERMS) for token in tokens[question_index + 1 :])
        and any(_token_matches_any(token, SALES_DISCOVERY_VERBS) for token in tokens[question_index + 1 :])
    ):
        return True
    if not contains_any_phrase(cleaned, SALES_DISCOVERY_CATALOG_FALLBACK_TERMS):
        return False
    verbs = "|".join(re.escape(verb) for verb in SALES_DISCOVERY_VERBS)
    pattern = (
        r"(?:^|\b)"
        r"(?:(?:hola|buenas|buenos dias|buenas tardes|buenas noches|holi|hello|hi)\s+)?"
        r"(?:que)\b(?:\s+[a-z0-9]+){0,3}\s+"
        rf"(?:{verbs})\b"
    )
    return bool(re.search(pattern, cleaned))


def _is_agency_request_normalized(normalized: str) -> bool:
    """Indica si normalized agency request se cumple."""
    return _contains_any(normalized, SALES_AGENCY_TERMS)


def _looks_like_plan_request_normalized(normalized: str) -> bool:
    """Devuelve el normalized looks like plan request."""
    if _is_discovery_query_normalized(normalized):
        return True
    return contains_any_phrase(normalized, SALES_CATALOG_TERMS)


def _has_explicit_catalog_terms_normalized(normalized: str) -> bool:
    """Indica si normalized explicit catalog terms se cumple."""
    return contains_any_phrase(normalized, EXPLICIT_SALES_CATALOG_TERMS)


def _detect_catalog_segment_normalized(normalized: str) -> str | None:
    """Detecta catalog segment normalized."""
    wants_residential = _contains_any(
        normalized,
        ("hogar", "residencial", "casa", "departamento", "domicilio", "familiar"),
    )
    wants_pymes = _contains_any(
        normalized,
        (
            "pyme",
            "pymes",
            "empresa",
            "empresas",
            "negocio",
            "negocios",
            "corporativo",
            "corporativa",
            "corporativos",
            "corporativas",
            "coorporativo",
            "coorporativa",
        ),
    )
    if wants_residential == wants_pymes:
        return None
    return "residential" if wants_residential else "pymes"


def _declines_registration_normalized(normalized: str) -> bool:
    """Devuelve el normalized declines registration."""
    if not normalized:
        return False
    if normalized in {"no", "nop", "no gracias", "prefiero no"}:
        return True
    if (
        _contains_any(normalized, ("no quiero", "no deseo", "prefiero no", "sin"))
        and _contains_any(
            normalized,
            (
                "registrar",
                "datos",
                "nombre",
                "telefono",
                "teléfono",
                "direccion",
                "dirección",
                "ubicacion",
                "ubicación",
                "compartir",
            ),
        )
    ):
        return True
    return _contains_any(normalized, SALES_REGISTRATION_DECLINE_TERMS)


def _looks_like_commercial_followup_normalized(normalized: str) -> bool:
    """Devuelve el normalized looks like commercial followup."""
    cleaned = _clean_punctuation(normalized)
    if not cleaned:
        return False
    if contains_any_phrase(cleaned, COMMERCIAL_FOLLOWUP_TERMS):
        return True
    if CHEAPNESS_PATTERN.search(cleaned) or LOW_PRICE_PATTERN.search(cleaned) or ECONOMIC_PATTERN.search(cleaned):
        return True
    return bool(COMMERCIAL_BANDWIDTH_PATTERN.search(cleaned))


def _is_generic_sales_opening_normalized(normalized: str) -> bool:
    """Indica si normalized generic sales opening se cumple."""
    if not normalized:
        return False
    if _is_discovery_query_normalized(normalized):
        return True
    if _contains_any(normalized, SALES_GENERIC_OPENING_BLOCKERS):
        return False
    return _contains_any(normalized, SALES_GENERIC_OPENING_TERMS)


def _looks_like_agency_followup_normalized(normalized: str) -> bool:
    """Devuelve el normalized looks like agency followup."""
    cleaned = _clean_punctuation(normalized)
    if not cleaned:
        return False
    if contains_any_phrase(cleaned, SALES_CATALOG_TERMS):
        return False
    if any(province in cleaned for province in AGENCY_FOLLOWUP_PROVINCES):
        return True
    if AGENCY_FOLLOWUP_PATTERN.match(cleaned):
        return True
    return bool(AGENCY_FOLLOWUP_SECONDARY_PATTERN.match(cleaned))


def _wants_capture_normalized(normalized: str) -> bool:
    """Devuelve el normalized wants capture."""
    return _contains_any(normalized, SALES_CAPTURE_TERMS)


def _wants_full_catalog_normalized(normalized: str) -> bool:
    """Devuelve el normalized wants full catalog."""
    return _contains_any(normalized, SALES_FULL_CATALOG_TERMS)


def _accepts_recommended_plan_normalized(normalized: str) -> bool:
    """Devuelve el normalized accepts recommended plan."""
    cleaned = _clean_punctuation(normalized)
    if not cleaned:
        return False
    if _contains_any(cleaned, SALES_RECOMMENDED_PLAN_REJECT_TERMS):
        return False
    if cleaned in SALES_RECOMMENDED_PLAN_ACCEPT_EXACT:
        return True
    return _contains_any(cleaned, SALES_RECOMMENDED_PLAN_ACCEPT_TERMS)


def _wants_personalized_recommendation_normalized(normalized: str) -> bool:
    """Devuelve el normalized wants personalized recommendation."""
    return _contains_any(normalized, SALES_RECOMMENDATION_CHOICE_TERMS)


def _detect_sales_intent_normalized(normalized: str) -> str:
    """Detecta sales intent normalized."""
    if _contains_any(normalized, SALES_MENU_TERMS):
        return "menu"
    if _is_discovery_query_normalized(normalized):
        return "discovery"
    if _is_agency_request_normalized(normalized):
        return "agencies"
    if _contains_any(normalized, SALES_PAYMENT_METHOD_TERMS):
        return "payment_methods"
    if _contains_any(normalized, SALES_MONTHLY_PAYMENT_TERMS):
        return "pago_mensual"
    if _contains_any(
        normalized,
        (
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
            "reclamo",
            "reclamos",
            "soy cliente",
        ),
    ):
        return "payments"
    if _contains_any(normalized, SALES_INFO_TERMS):
        return "info"
    if _looks_like_commercial_followup_normalized(normalized):
        return "plan"
    if _looks_like_plan_request_normalized(normalized):
        return "plan"
    if _contains_any(normalized, SALES_INSTALLATION_TIME_TERMS):
        return "instalacion_tiempo"
    if _contains_any(normalized, SALES_INSTALLATION_COST_TERMS):
        return "instalacion_costo"
    if _contains_any(normalized, SALES_REQUIREMENTS_TERMS):
        return "requisitos"
    if _contains_any(normalized, SALES_CONTRACT_TERMS):
        return "contrato"
    if _contains_any(normalized, SALES_CONTACT_TERMS):
        return "contacto"
    if _contains_any(normalized, SALES_WARRANTY_TERMS):
        return "garantia"
    if _is_greeting_normalized(normalized):
        return "greeting"
    return "generic"


def analyze_sales_message(text: str | None) -> SalesMessageAnalysis:
    """Analiza mensaje de sales."""
    normalized = normalize_text(text)
    return SalesMessageAnalysis(
        normalized=normalized,
        intent=_detect_sales_intent_normalized(normalized),
        catalog_segment=_detect_catalog_segment_normalized(normalized),
        is_greeting=_is_greeting_normalized(normalized),
        is_discovery_query=_is_discovery_query_normalized(normalized),
        is_generic_opening=_is_generic_sales_opening_normalized(normalized),
        is_commercial_followup=_looks_like_commercial_followup_normalized(normalized),
        declines_registration=_declines_registration_normalized(normalized),
        is_agency_followup=_looks_like_agency_followup_normalized(normalized),
        has_explicit_catalog_terms=_has_explicit_catalog_terms_normalized(normalized),
        wants_capture=_wants_capture_normalized(normalized),
        wants_full_catalog=_wants_full_catalog_normalized(normalized),
        wants_personalized_recommendation=_wants_personalized_recommendation_normalized(normalized),
        accepts_recommended_plan=_accepts_recommended_plan_normalized(normalized),
    )


def detect_sales_intent(text: str | None) -> str:
    """Detecta sales intent."""
    return analyze_sales_message(text).intent


def is_sales_greeting(text: str | None) -> bool:
    """Indica si greeting sales se cumple."""
    return analyze_sales_message(text).is_greeting


def looks_like_plan_request(text: str | None) -> bool:
    """Devuelve el request looks like plan."""
    normalized = normalize_text(text)
    return _looks_like_plan_request_normalized(normalized)


def detect_commercial_catalog_segment(text: str | None) -> str | None:
    """Detecta commercial catalog segment."""
    return analyze_sales_message(text).catalog_segment


def declines_commercial_registration(text: str | None) -> bool:
    """Devuelve el registration declines commercial."""
    return analyze_sales_message(text).declines_registration


def looks_like_commercial_followup(text: str | None) -> bool:
    """Devuelve el followup looks like commercial."""
    return analyze_sales_message(text).is_commercial_followup


def is_generic_sales_opening(text: str | None) -> bool:
    """Indica si opening generic sales se cumple."""
    return analyze_sales_message(text).is_generic_opening


def looks_like_agency_followup(text: str | None) -> bool:
    """Devuelve el followup looks like agency."""
    return analyze_sales_message(text).is_agency_followup
