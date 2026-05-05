"""Utilidades de normalización y coincidencia de texto."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

COMMON_TOKEN_ALIASES = {
    "q": "que",
    "k": "que",
    "ke": "que",
    "ola": "hola",
}


def normalize_text(value: str | None) -> str:
    """Normaliza texto."""
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()
    cleaned = re.sub(r"[^a-z0-9]+", " ", ascii_value)
    tokens = [COMMON_TOKEN_ALIASES.get(token, token) for token in cleaned.split()]
    return " ".join(tokens)


@lru_cache(maxsize=4096)
def _token_forms(token: str) -> tuple[str, ...]:
    """Genera formas comparables de un token para detectar coincidencias cercanas."""
    token = normalize_text(token)
    if not token:
        return ()
    forms = {token}
    alias = COMMON_TOKEN_ALIASES.get(token)
    if alias:
        forms.add(alias)
    forms.add(re.sub(r"(.)\1{2,}", r"\1", token))
    return tuple(sorted(form for form in forms if form))


def _token_distance_lte(a: str, b: str, *, limit: int) -> bool:
    """Comprueba si dos tokens quedan dentro de la distancia permitida."""
    if a == b:
        return True
    if not a or not b:
        return False
    if abs(len(a) - len(b)) > limit:
        return False

    previous = list(range(len(b) + 1))
    current = [0] * (len(b) + 1)
    previous_previous: list[int] | None = None

    for i, char_a in enumerate(a, start=1):
        current[0] = i
        min_in_row = current[0]
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            deletion = previous[j] + 1
            insertion = current[j - 1] + 1
            substitution = previous[j - 1] + cost
            cell = min(deletion, insertion, substitution)
            if (
                previous_previous is not None
                and i > 1
                and j > 1
                and char_a == b[j - 2]
                and a[i - 2] == char_b
            ):
                cell = min(cell, previous_previous[j - 2] + 1)
            current[j] = cell
            if cell < min_in_row:
                min_in_row = cell
        if min_in_row > limit:
            return False
        previous_previous, previous, current = previous, current, previous
    return previous[-1] <= limit


def token_matches(actual: str | None, expected: str | None) -> bool:
    """Devuelve los matches token."""
    actual_forms = _token_forms(str(actual or ""))
    expected_forms = _token_forms(str(expected or ""))
    if not actual_forms or not expected_forms:
        return False
    for actual_form in actual_forms:
        for expected_form in expected_forms:
            if actual_form == expected_form:
                return True
            max_len = max(len(actual_form), len(expected_form))
            if max_len <= 3:
                continue
            if actual_form[0] != expected_form[0]:
                continue
            limit = 1 if max_len <= 7 else 2
            distance_matches = _token_distance_lte(actual_form, expected_form, limit=limit)
            if not distance_matches:
                continue
            if actual_form[-1] == expected_form[-1]:
                return True
            if max_len >= 6 and actual_form[:3] == expected_form[:3] and limit >= 1:
                return True
    return False


def matches_phrase(text: str | None, phrase: str | None) -> bool:
    """Devuelve el phrase matches."""
    text_tokens = normalize_text(text).split()
    phrase_tokens = normalize_text(phrase).split()
    if not text_tokens or not phrase_tokens:
        return False
    if len(text_tokens) != len(phrase_tokens):
        return False
    return all(token_matches(actual, expected) for actual, expected in zip(text_tokens, phrase_tokens))


def contains_phrase(text: str | None, phrase: str | None) -> bool:
    """Devuelve el phrase contains."""
    normalized_text = normalize_text(text)
    normalized_phrase = normalize_text(phrase)
    if not normalized_text or not normalized_phrase:
        return False
    if normalized_phrase in normalized_text:
        return True

    text_tokens = normalized_text.split()
    phrase_tokens = normalized_phrase.split()
    if len(phrase_tokens) > len(text_tokens):
        return False
    if len(phrase_tokens) == 1:
        expected = phrase_tokens[0]
        return any(token_matches(actual, expected) for actual in text_tokens)

    window_size = len(phrase_tokens)
    for index in range(len(text_tokens) - window_size + 1):
        window = text_tokens[index : index + window_size]
        if all(token_matches(actual, expected) for actual, expected in zip(window, phrase_tokens)):
            return True
    return False


def contains_any_phrase(text: str | None, phrases: tuple[str, ...] | list[str] | set[str]) -> bool:
    """Devuelve el phrase contains any."""
    return any(contains_phrase(text, phrase) for phrase in phrases)


def matches_any_phrase(text: str | None, phrases: tuple[str, ...] | list[str] | set[str]) -> bool:
    """Devuelve el phrase matches any."""
    return any(matches_phrase(text, phrase) for phrase in phrases)
