"""Mensajes y plantillas reutilizables del orquestador."""

from packages.shared.assistant_persona import (
    assistant_followup_prompt,
    assistant_generic_followup_prompt,
    assistant_generic_prompt,
    assistant_welcome_prompt,
)


def format_billing_proof_identity_request(*, intro: str = "") -> str:
    """Da formato a billing proof identity request para presentarlo de forma clara."""
    return (
        f"{intro}" if intro else ""
    ) + (
        "Ya vi la imagen y parece un comprobante de pago. "
        "Para revisarlo por aquí, compártame la cédula o RUC del titular del contrato."
    )


def format_human_handoff_identity_request() -> str:
    """Da formato a la solicitud de identidad previa a una derivación humana."""
    return (
        "Con gusto lo derivo con un asesor especializado. "
        "Para continuar, compártame la cédula o RUC del titular y dejo su caso listo."
    )


def format_missing_identity_message(
    *,
    pending_agent: str,
    attempts: int,
    pending_message: str | None = None,
    support_issue_type: str | None = None,
) -> str:
    """Da formato a mensaje de missing identity para presentarlo de forma clara."""
    if pending_agent == "support":
        support_tips = {
            "no_service": "Mientras la consigues, revisa que la ONU y el router estén encendidos y reinícialos 10 segundos.",
            "intermittence": "Mientras la consigues, reinicia la ONU y el router y revisa si el corte pasa en todos los equipos.",
            "slow_internet": "Mientras la consigue, reinicie la ONU y el router y pruebe si mejora en otro dispositivo.",
        }
        if attempts == 1:
            tip = support_tips.get(
                support_issue_type,
                "Mientras la consigue, puede probar reiniciando la ONU y el router.",
            )
            return (
                "Para revisar su servicio sí necesito la cédula o RUC del titular del contrato. "
                f"{tip} Cuando la tenga, me la envía y seguimos."
            )
        if attempts >= 3:
            return "Sin ese dato no puedo revisarlo por aquí. Cuando la tenga, me escribe y seguimos."
        return (
            "Sin la cédula o RUC del titular del contrato no puedo abrir la revisión por aquí. "
            "Cuando la tenga, me la envía y seguimos."
        )

    lowered_pending = str(pending_message or "").strip().lower()
    followup = ""
    if any(token in lowered_pending for token in ("pague", "pagado", "comprobante", "pago")):
        followup = " Si ya realizó el pago, cuando lo tenga también reviso su comprobante."
    if attempts == 1:
        return (
            "Para revisar valores, pagos o cortes sí necesito la cédula o RUC del titular del contrato. "
            f"Cuando la tenga, me la envía y seguimos.{followup}"
        )
    if attempts >= 3:
        return "Sin ese dato no puedo revisarlo por aquí. Cuando la tenga, me escribe y seguimos."
    return (
        "Sin la cédula o RUC del titular del contrato no puedo seguir con la revisión por aquí. "
        "Envíemela cuando la tenga."
    )


def format_identity_name_clarification(*, pending_agent: str) -> str:
    """Da formato a identity nombre clarification para presentarlo de forma clara."""
    if pending_agent == "billing":
        return (
            "Ese es el nombre del titular. Lo que necesito es la cédula o RUC "
            "para revisar valores, pagos o cortes."
        )
    return (
        "Ese es el nombre del titular. Lo que necesito es la cédula o RUC "
        "para revisar el servicio."
    )


def format_clarify_message(
    *,
    reason: str,
    intro: str = "",
    assistant_profile: dict | None = None,
    normalized_user_message: str = "",
    user_declines_assistance: bool = False,
) -> str:
    """Da formato a mensaje de clarify para presentarlo de forma clara."""
    if reason == "message_too_long":
        return (
            f"{intro}" if intro else ""
        ) + (
            "Envíeme su consulta en un mensaje más corto y la revisamos mejor. "
            "Si su consulta es por internet, facturación o planes, continúo con su atención."
        )
    if reason == "message_noise":
        return (
            f"{intro}" if intro else ""
        ) + (
            "No entendí bien su mensaje porque llegó con mucho texto repetido o ruido. "
            "Envíemelo otra vez en una sola frase, por favor. "
            "Si su consulta es por internet, facturación o planes, lo revisamos."
        )
    if reason == "greeting_only":
        if intro:
            return f"{intro}{assistant_welcome_prompt(assistant_profile)}"
        return assistant_followup_prompt(assistant_profile)
    if reason == "out_of_scope":
        return (
            f"{intro}" if intro else ""
        ) + (
            "Por ese tema no puedo ayudarle aquí. "
            "Si su consulta es por internet, facturación o planes, escríbamela en una sola frase y seguimos."
        )
    if reason == "small_talk":
        if any(token in normalized_user_message for token in ("gracias", "muchas gracias", "thanks")):
            return "Con gusto. Aquí continúo. Si luego su consulta es por internet, pagos o planes, lo revisamos."
        return "Todo bien por aquí, gracias. Si su consulta es por internet, pagos o planes, indíqueme."
    if user_declines_assistance:
        return "Está bien. Si más tarde necesita soporte, facturación o planes, aquí continúo."
    if intro:
        return f"{intro}{assistant_generic_prompt(assistant_profile)}"
    return assistant_generic_followup_prompt(assistant_profile)


def format_system_error_handoff_message() -> str:
    """Da formato a mensaje de system error handoff para presentarlo de forma clara."""
    return "Tuvimos un inconveniente interno y voy a dejar su caso con un asesor especializado para que continúe con su atención."


def format_system_error_handoff_failure_message() -> str:
    """Da formato a mensaje de system error handoff failure para presentarlo de forma clara."""
    return (
        "Estoy teniendo un inconveniente interno y ahora mismo no pude dejar su caso con un asesor especializado. "
        "Por favor, vuelva a escribir en unos minutos."
    )


def format_openai_runtime_handoff_message() -> str:
    """Da formato a mensaje de openai runtime handoff para presentarlo de forma clara."""
    return (
        "Tuvimos un inconveniente temporal con el asistente y voy a dejar su caso con un asesor especializado "
        "para que continúe con su atención."
    )
