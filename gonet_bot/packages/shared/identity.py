"""Validación y extracción de documentos de identidad ecuatorianos."""

import re


def _is_valid_ecuador_cedula(digits: str) -> bool:
    """Indica si cedula valid ecuador se cumple."""
    if len(digits) != 10 or not digits.isdigit():
        return False
    province = int(digits[:2])
    third_digit = int(digits[2])
    if province < 1 or (province > 24 and province != 30):
        return False
    if third_digit >= 6:
        return False
    total = 0
    for index, raw_digit in enumerate(digits[:9]):
        digit = int(raw_digit)
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    check_digit = (10 - (total % 10)) % 10
    return check_digit == int(digits[9])


def is_valid_identity_document(value: str | None) -> bool:
    """Indica si document valid identity se cumple."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        return _is_valid_ecuador_cedula(digits)
    if len(digits) == 13:
        return _is_valid_ecuador_cedula(digits[:10]) and digits[10:] != "000"
    return False


def extract_identity_document(text: str | None) -> str | None:
    """Extrae identity document."""
    raw_text = str(text or "")
    for match in re.finditer(r"(?<!\d)(?:\d[\s.-]?){10,13}(?!\d)", raw_text):
        digits = re.sub(r"\D", "", match.group())
        if is_valid_identity_document(digits):
            return digits
    return None
