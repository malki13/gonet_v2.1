"""Mensajes y ayudas para el tramo de facturación del flujo de contacto."""

from packages.agents.contact_contract_utils import (
    contract_code,
    contract_display_name,
    contract_due_value,
    contract_status_label,
    format_money,
)
from packages.shared.utils import normalize_text


BILLING_BUTTON_UPLOAD = "Enviar comprobante"
BILLING_BUTTON_PAYMENT = "Realice su pago aquí"
OCR_TEMPORARY_ERROR_HINTS = (
    "timeout",
    "timed out",
    "processing timeout",
    "processing_timeout",
    "gateway timeout",
    "504",
    "unavailable",
    "temporarily unavailable",
    "service unavailable",
    "backend",
    "upstream",
)
OCR_RETRY_IMAGE_HINTS = (
    "borrosa",
    "borroso",
    "legible",
    "legibilidad",
    "foto",
    "imagen",
    "archivo",
    "documento incompleto",
    "encuadre",
    "crop",
)


def _billing_proof_visibility_hint() -> str:
    """Devuelve el hint billing proof visibility."""
    return "Procura que la foto o imagen sea clara y que se vea el número del documento, la fecha y el monto."


def payment_link(cedula: str | None) -> str:
    """Devuelve el link payment."""
    return "https://pagos.gonet.ec/payment"


def format_billing_options(contract: dict, cedula: str | None) -> str:
    """Da formato a billing options para presentarlo de forma clara."""
    code = contract_code(contract)
    due_value = contract_due_value(contract)
    name = contract_display_name(contract)
    status = contract_status_label(contract)
    intro = (
        f"{name}, ya revisé su contrato *{code}* y aparece *{status}*."
        if name
        else f"Ya revisé su contrato *{code}* y aparece *{status}*."
    )
    lines = [intro]
    if due_value > 0:
        lines.append(f"Registra un pago pendiente de *${format_money(due_value)} más impuestos*.")
    lines.append("Si ya realizó el pago, envíeme el comprobante y lo reviso.")
    lines.append(f"Si prefiere pagar ahora, Aquí tiene el enlace directo: {payment_link(cedula)}")
    return "\n".join(lines)


def format_billing_duplicate_message(*, escalate: bool = False) -> str:
    """Da formato al mensaje de comprobante duplicado."""
    base = (
        "Ya validé el comprobante, pero ese pago ya está registrado y no es válido como nuevo comprobante."
    )
    if escalate:
        return base + " Voy a dejar su caso con un asesor especializado para continuar con la revisión."
    return base + " Si tiene otro comprobante distinto, envíemelo y lo reviso."


def format_billing_action_nudge() -> str:
    """Da formato a billing action nudge para presentarlo de forma clara."""
    return 'Para continuar por aquí, escriba "Registrar Pago", "Link de Cobro" o "asesor especializado".'


def format_billing_proof_request() -> str:
    """Da formato a la solicitud de comprobante de pago para presentarlo de forma clara."""
    return (
        "Perfecto. Envíeme el comprobante de pago en una foto clara o PDF y lo reviso.\n\n"
        f"{_billing_proof_visibility_hint()}"
    )


def format_billing_proof_nudge() -> str:
    """Da formato a el recordatorio de comprobante de pago para presentarlo de forma clara."""
    return (
        "Aquí continúo pendiente del comprobante. Envíemelo en una foto clara o PDF.\n\n"
        f"{_billing_proof_visibility_hint()} Si lo prefiere, también puede escribir \"asesor especializado\"."
    )


def normalize_billing_action(text: str) -> str:
    """Normaliza accion de facturacion."""
    lowered = (text or "").strip().lower()
    if lowered == "1":
        return "Registrar Pago"
    if lowered == "2":
        return "Link de Cobro"
    if lowered in {
        "primero",
        "primera",
        "la primera",
        "opcion 1",
        "opción 1",
        "primera opcion",
        "primera opción",
        "la de registrar pago",
        "la de pago",
    }:
        return "Registrar Pago"
    if lowered in {
        "segundo",
        "segunda",
        "la segunda",
        "opcion 2",
        "opción 2",
        "segunda opcion",
        "segunda opción",
        "el link",
        "la del link",
        "el enlace",
        "la del enlace",
        "la otra",
    }:
        return "Link de Cobro"
    if any(
        token in lowered
        for token in [
            "registrar pago",
            "registrar mi pago",
            "validar pago",
            "comprobante",
            "ya pague",
            "ya pagué",
            "pague",
            "pagué",
            "realice el pago",
            "realicé el pago",
            "acabo de pagar",
            "subir pago",
            "reportar pago",
            "enviar comprobante",
            "enviar mi comprobante",
            "te paso el recibo",
            "te paso el comprobante",
            "te mando el recibo",
            "te mando el comprobante",
        ]
    ):
        return "Registrar Pago"
    if any(
        token in lowered
        for token in [
            "link de cobro",
            "link pago",
            "link de pago",
            "enlace de cobro",
            "enlace de pago",
            "pagar",
            "haz tu pago",
            "paga aquí",
            "paga aqui",
            "mandame el link",
            "mandame el enlace",
            "mándame el link",
            "mándame el enlace",
        ]
    ):
        return "Link de Cobro"
    return ""


def format_billing_receipt_summary(
    *,
    extracted: str | None,
    registration: dict | None = None,
    ocr_result: dict | None = None,
    compact: bool = False,
) -> str:
    """Da formato a billing receipt summary para presentarlo de forma clara."""
    resolved = ((registration or {}).get("resolved") or {}) if isinstance(registration, dict) else {}
    lines: list[str] = []
    code = resolved.get("code")
    if not code and isinstance(ocr_result, dict):
        code = ocr_result.get("documento")
    if code:
        lines.append(f"- Número detectado: {code}")
    value = resolved.get("value")
    if value in (None, "") and isinstance(ocr_result, dict):
        value = ocr_result.get("valor")
    if value not in (None, ""):
        lines.append(f"- Monto detectado: {format_money(value)}")
    detected_date = resolved.get("ocr_date") or resolved.get("date")
    if not detected_date and isinstance(ocr_result, dict):
        detected_date = ocr_result.get("fecha")
    if detected_date:
        lines.append(f"- Fecha detectada: {detected_date}")
    if compact:
        deposit = resolved.get("deposit") if isinstance(resolved.get("deposit"), dict) else {}
        if deposit.get("name"):
            lines.append(f"- Banco detectado: {deposit['name']}")
        balance_due = resolved.get("balance_due")
        if balance_due not in (None, "") and float(balance_due) > 0:
            lines.append(f"- Saldo pendiente: {format_money(balance_due)}")
        return "\n".join(lines)

    pending_value = resolved.get("pending_value")
    if pending_value not in (None, ""):
        lines.append(f"- Deuda total: {format_money(pending_value)}")
    balance_due = resolved.get("balance_due")
    if balance_due not in (None, "") and float(balance_due) > 0:
        lines.append(f"- Saldo pendiente: {format_money(balance_due)}")
    deposit = resolved.get("deposit") if isinstance(resolved.get("deposit"), dict) else {}
    if deposit.get("name"):
        lines.append(f"- Banco detectado: {deposit['name']}")
    preview = str(extracted or "").strip()
    if preview:
        lines.append(f"- Texto OCR: {preview[:240] + ('...' if len(preview) > 240 else '')}")
    return "\n".join(lines)


def format_billing_handoff_summary(
    *,
    reason: str,
    contract: dict,
    registration: dict | None = None,
    ocr_result: dict | None = None,
    proof_attempts: int = 0,
    proof_failures: list[str] | None = None,
) -> str:
    """Da formato a billing handoff summary para presentarlo de forma clara."""
    resolved = ((registration or {}).get("resolved") or {}) if isinstance(registration, dict) else {}
    lines = [
        "Caso: Facturación",
        "Subcaso: Validación de comprobante",
        f"Motivo de escalamiento: {reason}",
    ]
    if contract:
        lines.extend(
            [
                "",
                "Contrato:",
                f"- Contrato: {contract_code(contract)}",
                f"- Estado: {contract_status_label(contract)}",
            ]
        )
        pending_value = contract_due_value(contract)
        if pending_value > 0:
            lines.append(f"- Valor pendiente: {format_money(pending_value)}")
    ocr_lines: list[str] = []
    if proof_attempts > 0:
        ocr_lines.append(f"- Intentos OCR: {proof_attempts}")
    failure_history = [str(item).strip() for item in (proof_failures or []) if str(item).strip()]
    if failure_history:
        ocr_lines.append(f"- Historial OCR: {' | '.join(failure_history)}")
    ocr_status = str((ocr_result or {}).get("status") or "").strip()
    if ocr_status:
        ocr_lines.append(f"- Estado OCR: {ocr_status}")
    ocr_state = str((ocr_result or {}).get("estado") or "").strip()
    if ocr_state:
        ocr_lines.append(f"- Estado OCR proveedor: {ocr_state}")
    if (ocr_result or {}).get("debe_reintentar") is True:
        ocr_lines.append("- El proveedor OCR pidió reenviar la imagen")
    receipt_summary = format_billing_receipt_summary(
        extracted=(ocr_result or {}).get("texto_extraido") if isinstance(ocr_result, dict) else None,
        registration=registration,
        ocr_result=ocr_result,
    )
    if receipt_summary:
        ocr_lines.append(receipt_summary)
    if ocr_lines:
        lines.extend(["", "OCR:", *ocr_lines])

    registration_lines: list[str] = []
    status = (registration or {}).get("status")
    if status:
        registration_lines.append(f"- Estado registro: {status}")
    missing = sorted(set((registration or {}).get("missing") or []))
    if missing:
        registration_lines.append(f"- Campos faltantes: {', '.join(missing)}")
    date_diff_days = resolved.get("date_diff_days")
    max_date_diff_days = resolved.get("max_ocr_date_diff_days")
    if date_diff_days not in (None, "") and max_date_diff_days not in (None, ""):
        registration_lines.append(f"- Diferencia de fecha detectada: {date_diff_days} días (máximo {max_date_diff_days})")
    reconnect_error = resolved.get("reconnect_error")
    if reconnect_error:
        registration_lines.append(f"- Error técnico: {str(reconnect_error)[:220]}")
    if registration_lines:
        lines.extend(["", "Registro automático:", *registration_lines])
    return "\n".join(line for line in lines if line).strip()


def describe_billing_proof_failure(ocr_result: dict | None, attempt: int) -> str:
    """Describe por qué falló la validación del comprobante de pago."""
    status = str((ocr_result or {}).get("status") or "").strip()
    estado = str((ocr_result or {}).get("estado") or "").strip()
    if status == "duplicate" or estado == "duplicate":
        return f"Intento {attempt}: el pago ya estaba registrado y el comprobante no es válido como nuevo pago"
    if (ocr_result or {}).get("debe_reintentar") is True or status == "needs_better_image":
        return f"Intento {attempt}: OCR pidió reenviar la imagen por baja legibilidad ({estado or status or 'retry'})"
    if status:
        return f"Intento {attempt}: OCR no pudo validarlo automáticamente ({estado or status})"
    return f"Intento {attempt}: no se pudo validar el comprobante"


def classify_billing_proof_error_kind(ocr_result: dict | None) -> str:
    """Clasifica billing proof error kind."""
    if (ocr_result or {}).get("debe_reintentar") is True:
        return "retry_image"

    source = " ".join(
        str((ocr_result or {}).get(key) or "")
        for key in ("status", "estado", "code", "error", "message", "detail", "reason")
    )
    normalized = normalize_text(source)
    if not normalized:
        return "temporary"
    if any(hint in normalized for hint in OCR_RETRY_IMAGE_HINTS):
        return "retry_image"
    if any(hint in normalized for hint in OCR_TEMPORARY_ERROR_HINTS) or normalized in {"error", "unavailable"}:
        return "temporary"
    return "manual_review"


def format_billing_async_retry_message(*, kind: str, attempt: int) -> str:
    """Da formato a mensaje de billing async retry para presentarlo de forma clara."""
    if kind == "retry_image":
        if attempt <= 1:
            return (
                "Todavía no pude validarlo bien. ¿Podría reenviármelo una vez más, de preferencia con una foto clara "
                "y completa o como archivo/PDF, procurando que se vean el número del documento, la fecha y el monto?\n\n"
                f"{_billing_proof_visibility_hint()}"
            )
        return (
            "Todavía no pude validarlo bien. Si lo prefiere, reenvíemelo otra vez con mejor luz o como archivo/PDF, procurando "
            f"que se vean el número del documento, la fecha y el monto.\n\n{_billing_proof_visibility_hint()}"
        )
    return (
        "No pude validar el comprobante automáticamente esta vez. ¿Podría reenviármelo una vez más, de preferencia en una foto "
        f"clara o como archivo/PDF, procurando que se vean el número del documento, la fecha y el monto? "
        f"{_billing_proof_visibility_hint()} Si vuelve a fallar, un asesor especializado lo revisará."
    )
