"""Lógica de recomendación de planes y refinamiento del perfil del cliente."""

from __future__ import annotations

import re
from typing import Any

from packages.shared.sales_intents import detect_commercial_catalog_segment
from packages.shared.utils import normalize_text

RECOMMENDATION_FIELDS = ("segment", "people", "devices", "space_size", "usage")
PEOPLE_CONTEXT_TERMS = (
    "persona",
    "personas",
    "usuario",
    "usuarios",
    "miembro",
    "miembros",
)
PEOPLE_PREFIX_TERMS = (
    "somos",
    "familia de",
    "familia somos",
)
DEVICE_CONTEXT_TERMS = (
    "dispositivo",
    "dispositivos",
    "equipo",
    "equipos",
    "telefono",
    "telefonos",
    "movil",
    "moviles",
    "celular",
    "celulares",
    "tablet",
    "tablets",
    "tele",
    "teles",
    "tv",
    "computadora",
    "computadoras",
    "laptop",
    "laptops",
    "pc",
    "pcs",
)
DIRECT_ANSWER_PREFIX_PATTERN = (
    r"(?:para|como|unos?|unas?|aprox(?:imadamente)?|alrededor\s+de|mas\s+o\s+menos|"
    r"somos|seria|serian|por\s+ahi|diria|dirian)"
)
DIRECT_ANSWER_SUFFIX_PATTERN = r"(?:aprox(?:imadamente)?|mas\s+o\s+menos)"
NUMBER_WORDS = {
    "uno": 1,
    "una": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12,
    "trece": 13,
    "catorce": 14,
    "quince": 15,
    "dieciseis": 16,
    "dieciséis": 16,
    "diecisiete": 17,
    "dieciocho": 18,
    "diecinueve": 19,
    "veinte": 20,
}
USAGE_LABELS = {
    "basic": "navegación básica",
    "streaming": "entretenimiento y streaming",
    "remote_work": "teletrabajo o estudio",
    "gaming": "gaming",
    "business_ops": "oficina y sistemas del negocio",
    "cameras": "cámaras o monitoreo",
}
SPACE_LABELS = {"small": "pequeño", "medium": "mediano", "large": "grande"}
RECOMMENDATION_SEGMENTS = {"residential", "pymes"}
RECOMMENDATION_SPACE_VALUES = {"small", "medium", "large"}
RECOMMENDATION_USAGE_VALUES = set(USAGE_LABELS)
SEGMENT_ALIASES = {
    "residential": "residential",
    "hogar": "residential",
    "residencial": "residential",
    "casa": "residential",
    "departamento": "residential",
    "domicilio": "residential",
    "pymes": "pymes",
    "pyme": "pymes",
    "negocio": "pymes",
    "empresa": "pymes",
    "comercial": "pymes",
}
SPACE_SIZE_ALIASES = {
    "small": "small",
    "pequeno": "small",
    "pequena": "small",
    "compacto": "small",
    "compacta": "small",
    "departamento": "small",
    "medium": "medium",
    "mediano": "medium",
    "mediana": "medium",
    "normal": "medium",
    "large": "large",
    "grande": "large",
    "amplio": "large",
    "amplia": "large",
}
USAGE_ALIASES = {
    "basic": "basic",
    "basico": "basic",
    "navegacion basica": "basic",
    "streaming": "streaming",
    "entretenimiento": "streaming",
    "peliculas": "streaming",
    "series": "streaming",
    "remote work": "remote_work",
    "remote_work": "remote_work",
    "teletrabajo": "remote_work",
    "estudio": "remote_work",
    "teletrabajo o estudio": "remote_work",
    "gaming": "gaming",
    "gamer": "gaming",
    "juegos": "gaming",
    "business_ops": "business_ops",
    "oficina": "business_ops",
    "negocio": "business_ops",
    "sistemas": "business_ops",
    "cameras": "cameras",
    "camaras": "cameras",
    "monitoreo": "cameras",
    "seguridad": "cameras",
}


def _contains_term(text: str, terms: tuple[str, ...]) -> bool:
    """Devuelve el term contains."""
    return any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms)


def _normalize_choice(value: Any, aliases: dict[str, str]) -> str | None:
    """Normaliza choice."""
    normalized = normalize_text(value)
    if not normalized:
        return None
    return aliases.get(normalized)


def _number_word_pattern() -> str:
    """Devuelve el pattern number word."""
    return "|".join(sorted((re.escape(word) for word in NUMBER_WORDS), key=len, reverse=True))


NUMBER_TOKEN_PATTERN = re.compile(rf"\b(\d{{1,3}}|{_number_word_pattern()})\b")


def _parse_number_token(token: str | None) -> int | None:
    """Devuelve el token parse number."""
    cleaned = normalize_text(token)
    if not cleaned:
        return None
    if cleaned.isdigit():
        return int(cleaned)
    return NUMBER_WORDS.get(cleaned)


def parse_recommendation_number(value: Any) -> int | None:
    """Devuelve el number parse recommendation."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 100 else None
    if isinstance(value, float):
        rounded = int(round(value))
        return rounded if 1 <= rounded <= 100 else None
    normalized = normalize_text(value)
    if not normalized:
        return None
    direct = _extract_relaxed_direct_answer_number(normalized)
    if direct is not None:
        return direct if 1 <= direct <= 100 else None
    match = NUMBER_TOKEN_PATTERN.fullmatch(normalized)
    if not match:
        return None
    parsed = _parse_number_token(match.group(1))
    return parsed if parsed is not None and 1 <= parsed <= 100 else None


def _extract_relaxed_direct_answer_number(text: str) -> int | None:
    """Extrae relaxed direct answer number."""
    if not text:
        return None
    token_pattern = rf"(\d{{1,3}}|{_number_word_pattern()})"
    prefix = rf"(?:{DIRECT_ANSWER_PREFIX_PATTERN}\s+)?"
    suffix = rf"(?:\s+{DIRECT_ANSWER_SUFFIX_PATTERN})?"

    range_match = re.fullmatch(
        rf"{prefix}{token_pattern}\s*(?:-|/|a|o|hasta|\s)\s*{token_pattern}{suffix}",
        text,
    )
    if range_match:
        low = _parse_number_token(range_match.group(1))
        high = _parse_number_token(range_match.group(2))
        candidates = [value for value in (low, high) if value is not None]
        if candidates:
            return max(candidates)

    single_match = re.fullmatch(
        rf"{prefix}{token_pattern}{suffix}",
        text,
    )
    if single_match:
        return _parse_number_token(single_match.group(1))
    return None


def _is_number_only_reply(text: str) -> bool:
    """Indica si reply number only se cumple."""
    cleaned = (text or "").strip()
    return bool(cleaned) and bool(re.fullmatch(r"\d{1,3}", cleaned))


def _is_number_word_only_reply(text: str) -> bool:
    """Indica si reply number word only se cumple."""
    cleaned = (text or "").strip()
    return cleaned in NUMBER_WORDS


def _extract_contextual_number(
    text: str,
    *,
    suffix_terms: tuple[str, ...],
    prefix_terms: tuple[str, ...] = (),
    prefer_direct_answer: bool = False,
    blocked_terms: tuple[str, ...] = (),
    aggregate: str = "max",
) -> int | None:
    """Extrae contextual number."""
    if not text:
        return None

    candidates: list[int] = []
    suffix_pattern = "|".join(sorted((re.escape(term) for term in suffix_terms), key=len, reverse=True))
    prefix_pattern = "|".join(sorted((re.escape(term) for term in prefix_terms), key=len, reverse=True)) if prefix_terms else ""

    for match in re.finditer(rf"\b(\d{{1,3}}|{_number_word_pattern()})\s+(?:{suffix_pattern})\b", text):
        value = _parse_number_token(match.group(1))
        if value is not None:
            candidates.append(value)

    if prefix_pattern:
        for match in re.finditer(rf"\b(?:{prefix_pattern})\s+(\d{{1,3}}|{_number_word_pattern()})\b", text):
            value = _parse_number_token(match.group(1))
            if value is not None:
                candidates.append(value)

    if candidates:
        if aggregate == "sum" and len(candidates) > 1:
            return sum(candidates)
        return max(candidates)

    if blocked_terms and _contains_term(text, blocked_terms):
        return None

    if prefer_direct_answer:
        relaxed = _extract_relaxed_direct_answer_number(text)
        if relaxed is not None:
            return relaxed
    if prefer_direct_answer and _is_number_only_reply(text):
        return _parse_number_token(text)
    if prefer_direct_answer and _is_number_word_only_reply(text):
        return _parse_number_token(text)
    return None


def _extract_people(text: str, *, prefer_direct_answer: bool = False) -> int | None:
    """Extrae people."""
    return _extract_contextual_number(
        text,
        suffix_terms=PEOPLE_CONTEXT_TERMS,
        prefix_terms=PEOPLE_PREFIX_TERMS,
        prefer_direct_answer=prefer_direct_answer,
        blocked_terms=DEVICE_CONTEXT_TERMS,
    )


def _extract_devices(text: str, *, prefer_direct_answer: bool = False) -> int | None:
    """Extrae devices."""
    devices = _extract_contextual_number(
        text,
        suffix_terms=DEVICE_CONTEXT_TERMS,
        prefer_direct_answer=prefer_direct_answer,
        blocked_terms=PEOPLE_CONTEXT_TERMS,
        aggregate="sum",
    )
    if devices is not None:
        return devices
    if prefer_direct_answer and any(term in text for term in ("pocos", "poquitos")):
        return 4
    if prefer_direct_answer and "varios" in text:
        return 8
    if prefer_direct_answer and any(term in text for term in ("muchos", "bastantes")):
        return 15
    return None


def _parse_space_size(text: str) -> str | None:
    """Devuelve el size parse space."""
    if any(term in text for term in ("pequena", "pequeña", "chica", "chico", "compacta", "compacto", "departamento")):
        return "small"
    if any(term in text for term in ("mediana", "mediano", "normal")):
        return "medium"
    if any(term in text for term in ("grande", "amplia", "amplio", "casa grande", "local grande")):
        return "large"
    return None


def _parse_usage(text: str) -> str | None:
    """Devuelve el usage parse."""
    if _contains_term(text, ("gaming", "gamer", "jugar", "juegos", "xbox", "play", "playstation", "ps5")):
        return "gaming"
    if _contains_term(text, ("camaras", "cámaras", "seguridad", "cctv", "monitoreo")):
        return "cameras"
    if _contains_term(text, ("teletrabajo", "trabajo remoto", "zoom", "meet", "clases", "estudio", "estudiar")):
        return "remote_work"
    if _contains_term(text, ("oficina", "oficinas", "negocio", "empresa", "sistema", "sistemas", "erp", "facturacion", "facturación", "pos")):
        return "business_ops"
    if _contains_term(text, ("streaming", "entretenimiento", "netflix", "youtube", "peliculas", "películas", "series", "tv")):
        return "streaming"
    if _contains_term(text, ("basico", "básico", "navegar", "redes", "whatsapp", "correo")):
        return "basic"
    return None


def extract_recommendation_slot_updates(
    text: str | None,
    *,
    current_field: str | None = None,
) -> dict[str, Any]:
    """Extrae recommendation slot updates."""
    normalized = normalize_text(text)
    updates: dict[str, Any] = {}
    segment = detect_commercial_catalog_segment(normalized)
    if segment:
        updates["segment"] = segment
    people = _extract_people(normalized, prefer_direct_answer=current_field == "people")
    if people is not None:
        updates["people"] = people
    devices = _extract_devices(normalized, prefer_direct_answer=current_field == "devices")
    if devices is not None:
        updates["devices"] = devices
    space_size = _parse_space_size(normalized)
    if space_size:
        updates["space_size"] = space_size
    usage = _parse_usage(normalized)
    if usage:
        updates["usage"] = usage
    return updates


def sanitize_recommendation_slot_updates(updates: dict[str, Any] | None) -> dict[str, Any]:
    """Devuelve los updates sanitize recommendation slot."""
    raw = dict(updates or {})
    sanitized: dict[str, Any] = {}

    segment = _normalize_choice(raw.get("segment"), SEGMENT_ALIASES)
    if segment in RECOMMENDATION_SEGMENTS:
        sanitized["segment"] = segment

    people = parse_recommendation_number(raw.get("people"))
    if people is not None:
        sanitized["people"] = people

    devices = parse_recommendation_number(raw.get("devices"))
    if devices is not None:
        sanitized["devices"] = devices

    space_size = _normalize_choice(raw.get("space_size"), SPACE_SIZE_ALIASES)
    if space_size in RECOMMENDATION_SPACE_VALUES:
        sanitized["space_size"] = space_size

    usage = _normalize_choice(raw.get("usage"), USAGE_ALIASES)
    if usage in RECOMMENDATION_USAGE_VALUES:
        sanitized["usage"] = usage

    return sanitized


def merge_recommendation_profile(
    profile: dict[str, Any] | None,
    text: str | None,
    *,
    current_field: str | None = None,
    slot_updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fusiona el perfil de recomendación."""
    merged = dict(profile or {})
    updates = extract_recommendation_slot_updates(text, current_field=current_field)
    if slot_updates:
        updates.update(sanitize_recommendation_slot_updates(slot_updates))
    merged.update(updates)
    return merged


def next_recommendation_field(profile: dict[str, Any] | None) -> str | None:
    """Devuelve el field next recommendation."""
    profile = profile or {}
    for field in RECOMMENDATION_FIELDS:
        if not profile.get(field):
            return field
    return None


def recommendation_question(field: str, profile: dict[str, Any] | None = None) -> str:
    """Devuelve la pregunta recommendation."""
    segment = str((profile or {}).get("segment") or "").strip().lower()
    if field == "segment":
        return "¿El internet sería para su casa o para su negocio?"
    if field == "people":
        if segment == "pymes":
            return "Perfecto. Aproximadamente, ¿para cuántas personas sería en el negocio?"
        return "Perfecto. Aproximadamente, ¿para cuántas personas sería?"
    if field == "devices":
        return "Y aproximadamente, ¿cuántos dispositivos se conectarían al mismo tiempo?"
    if field == "space_size":
        if segment == "pymes":
            return "Listo. Y el espacio donde se usaría el servicio, ¿sería pequeño, mediano o grande?"
        return "Listo. Y el espacio donde iría el servicio, ¿sería pequeño, mediano o grande?"
    if field == "usage":
        if segment == "pymes":
            return "Y el uso principal, ¿sería más para oficina, videollamadas/sistemas, cámaras o algo más pesado?"
        return "Y el uso principal, ¿sería más para navegación básica, streaming, teletrabajo/estudio o gaming?"
    return "Indíqueme un poco más para poder recomendarle una opción puntual."


def _question_without_intro(text: str) -> str:
    """Devuelve el intro pregunta without."""
    cleaned = str(text or "").strip()
    return re.sub(r"^(?:perfecto|listo)\.?\s*", "", cleaned, flags=re.IGNORECASE).strip()


def _recommendation_field_ack(field: str | None, profile: dict[str, Any] | None) -> str:
    """Devuelve el ack recommendation field."""
    profile = profile or {}
    if field == "segment":
        segment = str(profile.get("segment") or "").strip().lower()
        if segment == "pymes":
            return "Bien, entonces sería para su negocio."
        if segment == "residential":
            return "Bien, entonces sería para su casa."
    if field == "people":
        people = profile.get("people")
        if people == 1:
            return "Perfecto, tomo como referencia 1 persona."
        if people:
            return f"Perfecto, tomo como referencia {people} personas."
    if field == "devices":
        devices = profile.get("devices")
        if devices:
            label = "dispositivo" if devices == 1 else "dispositivos"
            return f"Perfecto, tomo como referencia {devices} {label} conectados al mismo tiempo."
    if field == "space_size":
        space_label = SPACE_LABELS.get(str(profile.get("space_size") or "").lower())
        if space_label:
            return f"Bien, lo tomo como un espacio {space_label}."
    if field == "usage":
        usage_label = USAGE_LABELS.get(str(profile.get("usage") or "").lower())
        if usage_label:
            return f"Perfecto, con ese uso principal de {usage_label} ya puedo recomendarle algo más puntual."
    return ""


def _recommendation_transition_message(
    *,
    current_field: str | None,
    next_field: str,
    profile: dict[str, Any] | None,
) -> str:
    """Devuelve el mensaje recommendation transition."""
    profile = profile or {}
    segment = str(profile.get("segment") or "").strip().lower()
    if current_field == "segment" and next_field == "people":
        if segment == "pymes":
            return "Bien. Si es para su negocio, ¿más o menos cuántas personas lo usarían?"
        if segment == "residential":
            return "Bien. Si es para su casa, ¿más o menos cuántas personas lo usarían?"
    if current_field == "people" and next_field == "devices":
        return "Perfecto. ¿Y más o menos cuántos dispositivos se conectarían al mismo tiempo?"
    if current_field == "devices" and next_field == "space_size":
        if segment == "pymes":
            return "Claro. ¿Y el espacio donde se usaría el servicio sería pequeño, mediano o grande?"
        return "Claro. ¿Y el lugar donde iría el servicio sería pequeño, mediano o grande?"
    if current_field == "space_size" and next_field == "usage":
        if segment == "pymes":
            return "Listo. ¿Y qué uso le darían más: oficina, videollamadas/sistemas, cámaras o algo más pesado?"
        return "Listo. ¿Y qué uso le darían más: navegación básica, streaming, teletrabajo/estudio o gaming?"
    return ""


def build_recommendation_step_message(
    *,
    current_field: str | None,
    next_field: str,
    profile: dict[str, Any] | None,
) -> str:
    """Construye mensaje recommendation step a partir del contexto disponible."""
    transition = _recommendation_transition_message(
        current_field=current_field,
        next_field=next_field,
        profile=profile,
    )
    if transition:
        return transition
    ack = _recommendation_field_ack(current_field, profile)
    question = _question_without_intro(recommendation_question(next_field, profile))
    if ack and question:
        return f"{ack}\n\n{question}"
    return question or recommendation_question(next_field, profile)


def build_recommendation_retry_message(field: str | None, profile: dict[str, Any] | None = None) -> str:
    """Construye mensaje recommendation retry a partir del contexto disponible."""
    question = recommendation_question(field or "", profile)
    prompts = {
        "segment": "No me quedó claro si el internet sería para su casa o para su negocio.",
        "people": "No me quedó claro para cuántas personas sería el servicio.",
        "devices": "No me quedó claro cuántos dispositivos se conectarían al mismo tiempo.",
        "space_size": "No me quedó claro el tamaño del espacio donde iría el servicio.",
        "usage": "No me quedó claro cuál sería el uso principal que le darían.",
    }
    prefix = prompts.get(field or "", "No me quedó claro ese dato todavía.")
    return f"{prefix}\n\n{_question_without_intro(question) or question}".strip()


def build_recommended_plan_followup_prompt(plan: dict[str, Any] | None, profile: dict[str, Any] | None) -> str:
    """Construye prompt recommended plan followup a partir del contexto disponible."""
    plan = plan or {}
    profile = profile or {}
    name = str(plan.get("name") or "ese plan").strip()
    summary = _profile_summary(profile)
    base = f"Por lo que me comentó, **{name}** sí puede funcionarle bien"
    if summary and summary != "por el uso que me comentó":
        base = f"{base} {summary}"
    return (
        f"{base}.\n\n"
        f"Si lo prefiere, avanzamos con **{name}** y revisamos cobertura. "
        "Si prefiere comparar, le muestro los demás. ¿Cómo desea continuar?"
    )


def build_recommended_plan_capture_prefix(plan: dict[str, Any] | None, profile: dict[str, Any] | None) -> str:
    """Construye prefijo para capturar el plan recomendado a partir del contexto disponible."""
    plan = plan or {}
    name = str(plan.get("name") or "").strip()
    segment_label = "hogar" if str((profile or {}).get("segment") or "").lower() != "pymes" else "pyme"
    if name:
        return (
            f"Perfecto. Por lo que me comentó, **{name}** encaja bien para **{segment_label}**. "
            "Si lo prefiere, lo dejamos avanzado de una vez.\n\n"
            "Para revisar cobertura, sí voy a solicitarle unos datos básicos."
        )
    return (
        "Perfecto. Si lo prefiere, lo dejamos avanzado de una vez.\n\n"
        "Para revisar cobertura, sí voy a solicitarle unos datos básicos."
    )


def _sorted_items_for_segment(data: dict, segment: str | None) -> list[dict[str, Any]]:
    """Devuelve el segment sorted items for."""
    if segment == "pymes":
        items = (data.get("data") or {}).get("PYMES") or []
    else:
        items = (data.get("data") or {}).get("GONECTADOS") or []

    def speed(item: dict[str, Any]) -> int:
        """Devuelve el speed."""
        raw = str(item.get("mbps") or "").strip()
        match = re.search(r"\d+", raw)
        return int(match.group(0)) if match else 0

    return sorted(items, key=speed)


def _devices_score(devices: int | None) -> int:
    """Devuelve el score dispositivos."""
    if devices is None:
        return 0
    if devices <= 4:
        return 0
    if devices <= 8:
        return 1
    if devices <= 15:
        return 2
    return 3


def _people_score(people: int | None) -> int:
    """Devuelve el score people."""
    if people is None:
        return 0
    if people <= 2:
        return 0
    if people <= 4:
        return 1
    if people <= 8:
        return 2
    return 3


def _size_score(space_size: str | None) -> int:
    """Devuelve el score size."""
    return {"small": 0, "medium": 1, "large": 2}.get(str(space_size or "").lower(), 1)


def _usage_score(usage: str | None, *, segment: str | None) -> int:
    """Devuelve el score usage."""
    residential = {"basic": 0, "streaming": 1, "remote_work": 2, "business_ops": 2, "cameras": 2, "gaming": 3}
    pymes = {"basic": 0, "streaming": 1, "remote_work": 2, "business_ops": 2, "gaming": 2, "cameras": 3}
    scores = pymes if segment == "pymes" else residential
    return scores.get(str(usage or "").lower(), 1)


def recommend_plan(data: dict, profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """Devuelve el plan recommend."""
    profile = profile or {}
    segment = str(profile.get("segment") or "residential").strip().lower() or "residential"
    items = _sorted_items_for_segment(data, segment)
    if not items:
        return None
    load_score = max(_people_score(profile.get("people")), _devices_score(profile.get("devices")))
    score = load_score + _size_score(profile.get("space_size")) + _usage_score(
        profile.get("usage"),
        segment=segment,
    )
    max_score = 8
    index = round((score / max_score) * max(len(items) - 1, 0))
    index = max(0, min(index, len(items) - 1))
    return items[index]


def _fmt_price(value: Any) -> str:
    """Devuelve el precio fmt."""
    if value in {None, ""}:
        return "-"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_details(details: list[dict[str, Any]] | None, *, limit: int = 3) -> str:
    """Devuelve los detalles fmt."""
    names = [str((item or {}).get("name") or "").strip() for item in (details or [])]
    names = [name for name in names if name][:limit]
    if not names:
        return "-"
    return ", ".join(names)


def _profile_summary(profile: dict[str, Any] | None) -> str:
    """Devuelve el resumen perfil."""
    profile = profile or {}
    parts: list[str] = []
    people = profile.get("people")
    devices = profile.get("devices")
    if people == 1:
        parts.append("para **1 persona**")
    elif people:
        parts.append(f"para **{people} personas**")
    if devices:
        device_label = "dispositivo" if devices == 1 else "dispositivos"
        if people:
            parts.append(f"con unos **{devices} {device_label}**")
        else:
            parts.append(f"para unos **{devices} {device_label}**")
    space_label = SPACE_LABELS.get(str(profile.get("space_size") or "").lower())
    if space_label:
        parts.append(f"en un espacio **{space_label}**")
    usage_label = USAGE_LABELS.get(str(profile.get("usage") or "").lower())
    if usage_label:
        parts.append(f"con uso principal de **{usage_label}**")
    if not parts:
        return "por el uso que me comentó"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + f" y {parts[-1]}"


def build_recommendation_message(plan: dict[str, Any], profile: dict[str, Any] | None) -> str:
    """Construye mensaje recommendation a partir del contexto disponible."""
    profile = profile or {}
    segment_label = "hogar" if str(profile.get("segment") or "").lower() != "pymes" else "pyme"
    name = str(plan.get("name") or "este plan").strip()
    mbps = str(plan.get("mbps") or "-").strip()
    price = _fmt_price(plan.get("price", plan.get("final_price", "-")))
    details = _fmt_details(plan.get("details") or [])
    summary = _profile_summary(profile)
    return (
        f"Por lo que me comenta, le recomendaría **{name}** para **{segment_label}**.\n\n"
        f"Se lo sugiero {summary}.\n\n"
        "**Resumen del plan:**\n"
        f"- **Velocidad:** **{mbps} Mbps**\n"
        f"- **Precio + IMP:** **${price}**\n"
        f"- **Incluye:** {details}\n\n"
        "Si este plan le resulta adecuado, lo dejo avanzado con este plan y revisamos cobertura en su sector. "
        "Para eso sí le pediría su nombre y la ubicación.\n\n"
        "Si prefiere comparar, también le comparto todas las opciones. "
        "¿Desea que avancemos con este o le muestro los demás?"
    )
