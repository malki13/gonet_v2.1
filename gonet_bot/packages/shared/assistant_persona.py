"""Reglas y plantillas que mantienen la voz del asistente consistente."""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

ASSISTANT_GREETING_STYLES: tuple[dict[str, str], ...] = (
    {
        "id": "warm_support",
        "intro": "{greeting}, le atiende {assistant_name} de GoNet.",
        "welcome_prompt": (
            "Indíqueme, ¿en qué puedo ayudarle hoy? "
            "Si su consulta es por internet, pagos o planes, con gusto le atiendo."
        ),
        "followup_prompt": "Aquí estoy. Indíqueme, ¿en qué puedo ayudarle?",
        "generic_prompt": (
            "Indíqueme qué necesita y lo revisamos. "
            "Si su consulta es por internet, facturación o planes, continúo con su atención."
        ),
        "generic_followup_prompt": (
            "Aquí continúo. Si su consulta es por internet, facturación o planes, indíqueme y seguimos."
        ),
    },
    {
        "id": "close_human",
        "intro": "{greeting}, soy {assistant_name} de GoNet.",
        "welcome_prompt": (
            "Indíqueme qué necesita. "
            "Si su consulta es por internet, pagos o planes, lo revisamos."
        ),
        "followup_prompt": "Le leo. Indíqueme qué necesita.",
        "generic_prompt": (
            "Indíqueme qué necesita y lo revisamos. "
            "Si su consulta es por internet, facturación o planes, continúo con su atención."
        ),
        "generic_followup_prompt": (
            "Le leo. Si su consulta es por internet, facturación o planes, lo revisamos."
        ),
    },
    {
        "id": "cordial",
        "intro": "{greeting}, soy {assistant_name} de GoNet.",
        "welcome_prompt": (
            "¿Cómo puedo ayudarle hoy? "
            "Si su consulta es por internet, pagos o planes, lo revisamos."
        ),
        "followup_prompt": "Aquí continúo. Indíqueme qué necesita.",
        "generic_prompt": (
            "Indíqueme qué necesita y lo revisamos. "
            "Si su consulta es por internet, facturación o planes, continúo con su atención."
        ),
        "generic_followup_prompt": (
            "Aquí continúo. Si su consulta es por internet, facturación o planes, escríbame y seguimos."
        ),
    },
    {
        "id": "personal_contact",
        "intro": "{greeting}, está hablando con {assistant_name} de GoNet.",
        "welcome_prompt": (
            "Indíqueme qué necesita. "
            "Si su consulta es por internet, pagos o planes, lo revisamos."
        ),
        "followup_prompt": "Indíqueme qué necesita y seguimos.",
        "generic_prompt": (
            "Indíqueme qué necesita y lo revisamos. "
            "Si su consulta es por internet, facturación o planes, continúo con su atención."
        ),
        "generic_followup_prompt": (
            "Indíqueme si su consulta es por internet, facturación o planes y seguimos."
        ),
    },
)
ASSISTANT_GREETING_STYLE_IDS = {style["id"] for style in ASSISTANT_GREETING_STYLES}


def _normalize_text(text: str | None) -> str:
    """Normaliza texto."""
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).strip().lower()


def _stable_index(seed: str, size: int) -> int:
    """Calcula un índice estable a partir del texto."""
    if size <= 0:
        return 0
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % size


def ecuador_greeting(now: datetime | None = None) -> str:
    """Devuelve un saludo según la hora de Ecuador."""
    current = now or datetime.now(ZoneInfo("America/Guayaquil"))
    hour = current.hour
    if 5 <= hour < 12:
        return "Buenos días"
    if 12 <= hour < 19:
        return "Buenas tardes"
    return "Buenas noches"


def pick_stable_option(options: list[str] | tuple[str, ...], *, seed: str | None) -> str | None:
    """Devuelve el option pick stable."""
    if not options:
        return None
    if not seed:
        return options[0]
    return options[_stable_index(seed, len(options))]


def ensure_assistant_greeting_style(
    assistant_profile: dict[str, Any] | None,
    *,
    seed: str | None = None,
) -> dict[str, str]:
    """Ajusta el saludo para mantener el estilo del asistente."""
    if isinstance(assistant_profile, dict):
        style_id = str(assistant_profile.get("greeting_style") or "").strip()
        if style_id in ASSISTANT_GREETING_STYLE_IDS:
            for style in ASSISTANT_GREETING_STYLES:
                if style["id"] == style_id:
                    return style
        if not seed:
            chosen = ASSISTANT_GREETING_STYLES[0]
        else:
            chosen = ASSISTANT_GREETING_STYLES[_stable_index(seed, len(ASSISTANT_GREETING_STYLES))]
        assistant_profile["greeting_style"] = chosen["id"]
        return chosen
    return ASSISTANT_GREETING_STYLES[0]


def assistant_intro_prefix(
    *,
    assistant_name: str | None,
    assistant_profile: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """Devuelve el prefix assistant intro."""
    name = str(assistant_name or "").strip()
    greeting = ecuador_greeting(now)
    if not name:
        return f"{greeting}. "
    style = ensure_assistant_greeting_style(assistant_profile)
    return f"{style['intro'].format(assistant_name=name, greeting=greeting)} "


def assistant_welcome_prompt(assistant_profile: dict[str, Any] | None = None) -> str:
    """Devuelve el prompt assistant welcome."""
    return ensure_assistant_greeting_style(assistant_profile)["welcome_prompt"]


def assistant_followup_prompt(assistant_profile: dict[str, Any] | None = None) -> str:
    """Devuelve el prompt assistant followup."""
    return ensure_assistant_greeting_style(assistant_profile)["followup_prompt"]


def assistant_generic_prompt(assistant_profile: dict[str, Any] | None = None) -> str:
    """Devuelve el prompt assistant generic."""
    return ensure_assistant_greeting_style(assistant_profile)["generic_prompt"]


def assistant_generic_followup_prompt(assistant_profile: dict[str, Any] | None = None) -> str:
    """Devuelve el prompt assistant generic followup."""
    return ensure_assistant_greeting_style(assistant_profile)["generic_followup_prompt"]


def message_has_assistant_intro(text: str | None, *, assistant_name: str | None = None) -> bool:
    """Devuelve el intro mensaje has assistant."""
    normalized = _normalize_text(text)
    if not normalized:
        return False
    intro_markers = (
        "buenos dias, le atiende ",
        "buenas tardes, le atiende ",
        "buenas noches, le atiende ",
        "buenos dias, soy ",
        "buenas tardes, soy ",
        "buenas noches, soy ",
        "buenos dias, esta hablando con ",
        "buenas tardes, esta hablando con ",
        "buenas noches, esta hablando con ",
        "hola, te atiende ",
        "hola, soy ",
        "buenas, te saluda ",
        "hola, estas hablando con ",
    )
    if not any(marker in normalized for marker in intro_markers):
        return False
    if assistant_name:
        normalized_name = _normalize_text(assistant_name)
        return f"{normalized_name} de gonet" in normalized
    return " de gonet" in normalized
