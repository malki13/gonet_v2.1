"""Utilidades para presentar y validar contratos del cliente."""

import json
import re
import unicodedata
from typing import Any


CUT_OFF_STATES = {"pending", "pendiente", "cortado", "cutoff", "corte", "suspended", "suspendido"}
ACTIVE_STATES = {"open", "opened", "abierto", "activo", "active"}
CONSENT_ACTIONS = {
    "type": "buttons",
    "buttons": [
        {"id": "ASISTENCIA_ACEPTO", "title": "ACEPTO"},
    ],
}


def format_contract_holder_identity_request(*, contract_not_found: bool = False) -> str:
    """Da formato a contract holder identity request para presentarlo de forma clara."""
    if contract_not_found:
        return (
            "No encontré contratos con ese documento. "
            "Si el servicio está a nombre de otra persona, compártame la cédula o RUC del titular y lo reviso."
        )
    return "Compártame la cédula o RUC del titular del contrato y lo reviso."


def user_cannot_provide_holder_document(text: str | None) -> bool:
    """Devuelve el document user cannot provide holder."""
    normalized = _normalize_conversation_text(text or "")
    if not normalized:
        return False
    direct_phrases = {
        "no tengo la cedula",
        "no tengo la cedula del titular",
        "no tengo cedula del titular",
        "no tengo el numero de cedula",
        "no tengo el numero de cedula del titular",
        "no tengo el numero del titular",
        "no tengo el numero de ruc",
        "no tengo el ruc",
        "no tengo el ruc del titular",
        "no cuento con la cedula",
        "no dispongo de la cedula",
        "no recuerdo la cedula del titular",
        "no recuerdo el numero de cedula del titular",
        "no se la cedula del titular",
        "no me se la cedula del titular",
        "no tengo ese dato",
        "no tengo ese numero",
        "no se ese numero",
    }
    if normalized in direct_phrases:
        return True
    patterns = [
        r"\bno\s+(?:tengo|recuerdo|se|sé|dispongo|cuento\s+con)\b.{0,24}\b(?:cedula|cedula\s+del\s+titular|ruc|numero\s+de\s+cedula|numero\s+del\s+titular)\b",
        r"\b(?:sin|no\s+tengo)\b.{0,24}\b(?:cedula|ruc|numero\s+de\s+cedula|numero\s+del\s+titular)\b",
    ]
    return any(re.search(pattern, normalized) for pattern in patterns)


def contract_display_name(item: Any) -> str:
    """Devuelve el nombre contract display."""
    raw = contract_partner_name(item)
    cleaned = " ".join(raw.split())
    if not cleaned:
        return ""
    if cleaned == cleaned.upper() or cleaned == cleaned.lower():
        return cleaned.title()
    return cleaned


def format_information_consent_prompt(contract: dict | None = None) -> str:
    """Da formato a prompt de information consent para presentarlo de forma clara."""
    name = contract_display_name(contract or {})
    prefix = f"{name}, " if name else ""
    return f"{prefix}ya encontré su contrato. ¿Me confirma si acepta el uso de la información de GoNet para continuar por aquí?"


def _normalize_conversation_text(text: str) -> str:
    """Normaliza texto conversation."""
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(ascii_value.split())


def user_accepts_information(text: str, metadata: dict[str, Any] | None = None) -> bool:
    """Devuelve el information user accepts."""
    meta = metadata or {}
    reply_id = str(meta.get("interactive_reply_id") or meta.get("postback_payload") or "").strip().upper()
    if reply_id == "ASISTENCIA_ACEPTO":
        return True
    lowered = _normalize_conversation_text(text)
    return lowered in {"acepto", "aceptar", "si acepto", "sí acepto"}


def normalize_contract_rows(data: Any) -> list[dict]:
    """Normaliza filas contract."""
    def _parse_item(item: Any) -> list[dict]:
        """Devuelve el item parse."""
        if isinstance(item, list):
            rows: list[dict] = []
            for sub_item in item:
                rows.extend(_parse_item(sub_item))
            return rows
        if isinstance(item, dict):
            if "response" in item:
                return _parse_item(item.get("response"))
            return [item]
        if isinstance(item, str):
            try:
                parsed = json.loads(item)
            except json.JSONDecodeError:
                return []
            return _parse_item(parsed)
        return []

    if isinstance(data, dict):
        return _parse_item(data)
    if isinstance(data, list):
        rows: list[dict] = []
        for item in data:
            rows.extend(_parse_item(item))
        return rows
    if isinstance(data, str):
        return _parse_item(data)
    return []


def parse_amount(value: Any) -> float:
    """Devuelve el amount parse."""
    if value in (None, "", False):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    match = re.search(r"-?\d[\d.,]*", text)
    if not match:
        return 0.0
    number = match.group()
    if "." in number and "," in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        decimals = number.rsplit(",", 1)[-1]
        number = number.replace(",", ".") if len(decimals) <= 2 else number.replace(",", "")
    elif "." in number:
        decimals = number.rsplit(".", 1)[-1]
        if len(decimals) > 2:
            number = number.replace(".", "")
    try:
        return float(number)
    except ValueError:
        return 0.0


def format_money(value: Any) -> str:
    """Da formato a money para presentarlo de forma clara."""
    return f"{parse_amount(value):.2f}"


def contract_code(item: Any) -> str:
    """Devuelve el codigo contract."""
    if isinstance(item, dict):
        code = item.get("code") or item.get("contrato") or item.get("contract") or item.get("contract_number") or ""
        return str(code).strip()
    return str(item or "").strip()


def contract_raw_state(item: Any) -> str:
    """Devuelve el estado contract raw."""
    if not isinstance(item, dict):
        return ""
    for key in ("state", "status", "contract_status", "service_status"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip().lower()
    return ""


def contract_due_value(item: Any) -> float:
    """Devuelve el valor contract due."""
    if not isinstance(item, dict):
        return 0.0
    return parse_amount(item.get("residual"))


def contract_requires_billing(item: Any) -> bool:
    """Devuelve el billing contract requires."""
    state = contract_raw_state(item)
    if state in CUT_OFF_STATES:
        return True
    if state in ACTIVE_STATES:
        return False
    return False


def contract_status_label(item: Any) -> str:
    """Devuelve la etiqueta contract status."""
    return "cortado" if contract_requires_billing(item) else "activo"


def contract_partner_name(item: Any) -> str:
    """Devuelve el nombre contract partner."""
    if not isinstance(item, dict):
        return ""
    partner = item.get("partner") if isinstance(item.get("partner"), dict) else {}
    return str(partner.get("name") or item.get("name") or item.get("partner_name") or "").strip()


def extract_contracts_from_info(info_out: dict) -> list[dict]:
    """Extrae contracts from info."""
    rows = normalize_contract_rows(info_out.get("data") if isinstance(info_out, dict) else None)
    contracts: list[dict] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        code = contract_code(item)
        if not code or code in seen:
            continue
        plan = item.get("plan") if isinstance(item.get("plan"), dict) else {}
        street = str(item.get("street") or "").strip()
        street2 = str(item.get("street2") or "").strip()
        contracts.append(
            {
                **item,
                "code": code,
                "plan_name": str(plan.get("name") or plan.get("local_name") or item.get("plan_name") or "").strip(),
                "plan_price": str(plan.get("price") or item.get("plan_price") or "").strip(),
                "address": " / ".join([value for value in [street, street2] if value]),
                "status_label": contract_status_label(item),
                "pending_value": contract_due_value(item),
            }
        )
        seen.add(code)
    return contracts


def match_contract_in_text(text: str, contracts: list[Any]) -> str:
    """Devuelve el texto match contract in."""
    if not text:
        return ""
    stripped = text.strip()
    lowered = _normalize_conversation_text(stripped)
    ordinal_map = {
        "primer": 1,
        "primero": 1,
        "primera": 1,
        "segundo": 2,
        "segunda": 2,
        "tercero": 3,
        "tercera": 3,
        "cuarto": 4,
        "cuarta": 4,
        "quinto": 5,
        "quinta": 5,
    }
    if stripped.isdigit():
        idx = int(stripped)
        if 1 <= idx <= len(contracts):
            return contract_code(contracts[idx - 1])
    for item in contracts:
        code = contract_code(item)
        if code and code.lower() in lowered:
            return code
    selection_patterns = [
        r"\b(?:contrato|opcion|opcion\s+numero|opcion\s+nro|numero|nro)\s*(?:#\s*)?(\d{1,2})\b",
        r"\b(?:el|la)\s+(\d{1,2})\b",
    ]
    for pattern in selection_patterns:
        match = re.search(pattern, lowered)
        if not match:
            continue
        idx = int(match.group(1))
        if 1 <= idx <= len(contracts):
            return contract_code(contracts[idx - 1])
    for token, idx in ordinal_map.items():
        if re.search(rf"\b{re.escape(token)}\b", lowered) and 1 <= idx <= len(contracts):
            return contract_code(contracts[idx - 1])
    if any(token in lowered for token in ("cortado", "corte", "suspendido", "con deuda", "deuda", "pendiente", "debe")):
        cut_off_matches = [item for item in contracts if contract_status_label(item) == "cortado"]
        if len(cut_off_matches) == 1:
            return contract_code(cut_off_matches[0])
    if any(token in lowered for token in ("activo", "activa", "al dia", "al día", "sin deuda")):
        active_matches = [item for item in contracts if contract_status_label(item) == "activo"]
        if len(active_matches) == 1:
            return contract_code(active_matches[0])
    if any(token in lowered for token in ("deuda", "pendiente", "saldo", "valor", "debe")):
        unique_pending = [item for item in contracts if contract_due_value(item) > 0]
        if len(unique_pending) == 1:
            return contract_code(unique_pending[0])
    amount_matches: list[dict] = []
    for raw_amount in re.findall(r"\d+(?:[.,]\d{1,2})", lowered):
        parsed_amount = parse_amount(raw_amount)
        if parsed_amount <= 0:
            continue
        matched = [
            item
            for item in contracts
            if abs(contract_due_value(item) - parsed_amount) < 0.011
        ]
        if len(matched) == 1:
            amount_matches.append(matched[0])
    unique_amount_matches: dict[str, dict] = {}
    for item in amount_matches:
        code = contract_code(item)
        if code:
            unique_amount_matches[code] = item
    if len(unique_amount_matches) == 1:
        return contract_code(next(iter(unique_amount_matches.values())))
    return ""


def find_contract_by_code(contracts: list[Any], code: str) -> dict | None:
    """Devuelve el codigo find contract by."""
    for item in contracts or []:
        if contract_code(item) == code:
            return item if isinstance(item, dict) else None
    return None


def format_contract_selection(contracts: list[Any]) -> str:
    """Da formato a contract selection para presentarlo de forma clara."""
    if not contracts:
        return "No pude identificar su contrato. ¿Podría indicarme el número de contrato?"
    name = contract_display_name(contracts[0]) if contracts else ""
    intro = (
        f"{name}, veo varios contratos asociados a su cédula. Para no equivocarme, indíqueme cuál revisamos:"
        if name
        else "Veo varios contratos asociados a su cédula. Para no equivocarme, indíqueme cuál revisamos:"
    )
    lines = [intro, "Respóndame con el número, por ejemplo *1* o *2*."]
    for index, item in enumerate(contracts, start=1):
        code = contract_code(item)
        plan_name = str(item.get("plan_name") or "").strip() if isinstance(item, dict) else ""
        plan_price = str(item.get("plan_price") or "").strip() if isinstance(item, dict) else ""
        address = str(item.get("address") or "").strip() if isinstance(item, dict) else ""
        status = contract_status_label(item)
        due_value = contract_due_value(item)
        parts = [f"{index}. *{code}*", f"Estado: *{status}*"]
        if due_value > 0:
            parts.append(f"Valor pendiente: *${format_money(due_value)} más impuestos*")
        if plan_name:
            parts.append(f"Plan: {plan_name}")
        if plan_price:
            parts.append(f"Precio: ${plan_price}")
        if address:
            parts.append(f"Dir: {address}")
        lines.append(" - ".join(parts))
    return "\n".join(lines)
