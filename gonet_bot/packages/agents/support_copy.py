"""Mensajes reutilizables del flujo de soporte."""

from packages.agents.contact_contract_utils import contract_code, contract_display_name, contract_due_value, format_money

SUPPORT_OPTIONS = "problemas de red, cambiar el wifi o hablar con un asesor especializado"


def format_support_options(contract: dict) -> str:
    """Da formato a support options para presentarlo de forma clara."""
    partner_name = contract_display_name(contract)
    code = contract_code(contract)
    intro = f"{partner_name}, ya revisé su contrato *{code}*" if partner_name else f"Ya revisé su contrato *{code}*"
    return (
        f"{intro} y está *activo*. "
        "Indíqueme qué necesita: si presenta problemas de red, si desea cambiar el nombre o la clave del wifi, "
        "o si prefiere un asesor especializado."
    )


def format_support_clarification(contract: dict) -> str:
    """Da formato a support clarification para presentarlo de forma clara."""
    code = contract_code(contract)
    return (
        f"Ya revisé su contrato *{code}*. "
        "Indíqueme qué está ocurriendo: si está sin internet, si el servicio se corta por momentos, si está lento, "
        "si desea cambiar la clave del wifi o si prefiere que lo derive con un asesor especializado."
    )


def format_support_issue_triage_reply(contract: dict) -> str:
    """Da formato a support issue triage reply para presentarlo de forma clara."""
    code = contract_code(contract)
    return (
        f"Ya revisé su contrato *{code}*. Hagamos una prueba rápida: "
        "reinicie manualmente la ONU y el router, espere unos segundos y vuelva a conectarlos. "
        "Si le es posible, revise también los cables o pruebe otro tomacorriente. "
        "Después indíqueme si sigue sin internet, si el servicio se corta por momentos o si está lento."
    )


def format_support_issue_nudge(contract: dict) -> str:
    """Da formato a support issue nudge para presentarlo de forma clara."""
    return "Aquí continúo. Indíqueme si se quedó sin internet, si el servicio se corta por momentos o si está lento."


def format_support_monitoring_reply(issue_type: str, contract: dict) -> str:
    """Da formato a support monitoring reply para presentarlo de forma clara."""
    code = contract_code(contract)
    intros = {
        "no_service": (
            f"Ya revisé su contrato *{code}*. "
            "No veo una caída base en la red. Revise una sola vez que la ONU y el router estén encendidos "
            "y que los cables estén bien conectados."
        ),
        "intermittence": (
            f"Ya revisé su contrato *{code}*. "
            "Reinicie la ONU y el router y, si le es posible, pruébelos en otro tomacorriente."
        ),
        "slow_internet": (
            f"Ya revisé su contrato *{code}*. "
            "Revise si la lentitud ocurre en todos los dispositivos o solo en uno y confirme si está en la red correcta."
        ),
        "generic_network": (
            f"Ya revisé su contrato *{code}*. "
            "Haga una prueba rápida con sus equipos e indíqueme si el problema continúa."
        ),
    }
    return (
        intros.get(issue_type, intros["generic_network"])
        + " Cuando lo revise, indíqueme si *sigue igual*. Si hace falta, lo derivo con un asesor especializado."
    )


def format_support_recovery_reply(contract: dict, *, proactive: bool = False) -> str:
    """Da formato a support recovery reply para presentarlo de forma clara."""
    code = contract_code(contract)
    if proactive:
        return (
            f"Ya hice un reinicio remoto de la ONU y del router del contrato *{code}*. "
            "Espere un momento y revise de nuevo su servicio. "
            "Si *sigue igual*, indíquemelo y lo derivo con un asesor especializado."
        )
    return (
        f"Detecté una alerta en la ONU del contrato *{code}* y ya hice un reinicio de la ONU y del router. "
        "Espere un momento y revise de nuevo su servicio. "
        "Si *sigue igual*, indíquemelo y lo derivo con un asesor especializado."
    )


def format_support_guided_handoff_reply(
    issue_type: str,
    contract: dict,
    *,
    rebooted_remotely: bool = False,
    manual_checks_completed: bool = False,
) -> str:
    """Da formato a support guided handoff reply para presentarlo de forma clara."""
    code = contract_code(contract)
    reboot_intro = (
        f"Ya hice un reinicio remoto de la ONU y del router del contrato *{code}*. "
        "Voy a dejar su caso con un asesor especializado para continuar con la revisión. "
    )
    after_checks_intro = f"Gracias por realizar esas validaciones. Voy a dejar su caso con un asesor especializado para continuar con la revisión del contrato *{code}*. "
    intros = {
        "no_service": (
            after_checks_intro
            + "Indique, por favor, si la ONU y el router encendieron correctamente, "
            "si probó los equipos en otro tomacorriente y si el inconveniente sigue igual."
            if manual_checks_completed
            else
            f"Voy a dejar su caso con un asesor especializado para continuar con la revisión del contrato *{code}*. "
            "Mientras avanzamos con esa revisión, verifique que la ONU y el router estén encendidos, "
            "desconecte y vuelva a conectar la alimentación de ambos equipos, "
            "revise que el conector de fibra y el cable de red estén bien ajustados "
            "y, si le es posible, pruebe los equipos en otro tomacorriente."
        ),
        "intermittence": (
            after_checks_intro
            + "Comente si la intermitencia ocurre en todos los dispositivos o solo en uno "
            "y si los equipos ya fueron probados en otro tomacorriente."
            if manual_checks_completed
            else (reboot_intro if rebooted_remotely else f"Voy a escalar su caso con un asesor especializado para continuar con la revisión del contrato *{code}*. ")
            + "Mientras tanto, pruebe ambos equipos en otro tomacorriente "
            "y verifique si las intermitencias ocurren en todos los dispositivos o solo en uno."
        ),
        "slow_internet": (
            after_checks_intro
            + "Indique si la lentitud ocurre en todos los dispositivos o solo en uno, "
            "si ya probó cerca del router y si afecta navegación general, IPTV, videollamadas o juegos."
            if manual_checks_completed
            else f"Voy a dejar su caso con un asesor especializado para continuar con la revisión del contrato *{code}*. "
            "Antes de continuar, reinicie la ONU y el router desconectándolos de la energía por unos segundos y vuelva a conectarlos, "
            "asegúrese de estar conectado a la red correcta, pruebe cerca del router, "
            "valide si la lentitud ocurre en todos los dispositivos o solo en uno "
            "y confirme si el inconveniente ocurre en navegación general, IPTV, videollamadas o juegos."
        ),
        "generic_network": (
            after_checks_intro
            + "Comente si el inconveniente es sin servicio, intermitencia o lentitud, "
            "y si afecta a todos los dispositivos o solo a uno."
            if manual_checks_completed
            else (reboot_intro if rebooted_remotely else f"Voy a dejar su caso con un asesor especializado para continuar con la revisión del contrato *{code}*. ")
            + "Mientras tanto, revise que la ONU y el router estén encendidos, "
            "verifique los cables, pruebe los equipos en otro tomacorriente "
            "y confirme si el inconveniente afecta a todos los dispositivos o solo a uno."
        ),
    }
    return intros.get(issue_type, intros["generic_network"])


def format_support_device_count_reply(issue_type: str, contract: dict, connected_devices: int | None) -> str:
    """Da formato a support device conteo reply para presentarlo de forma clara."""
    code = contract_code(contract)
    devices_text = "No pude determinar cuántos dispositivos están conectados en este momento."
    if connected_devices is not None:
        plural = "dispositivo" if connected_devices == 1 else "dispositivos"
        devices_text = f"Actualmente identifico *{connected_devices}* {plural} conectados a su red."
    prefix = (
        f"Por el monitoreo de lentitud no detecté una falla base en el contrato *{code}*."
        if issue_type == "slow_internet"
        else f"Por el monitoreo base del contrato *{code}* no veo una falla inmediata en la red."
    )
    return (
        f"{prefix} {devices_text} "
        "Indíqueme si el inconveniente ocurre en todos los dispositivos o solo en uno. "
        "Si después de eso *sigue igual*, lo derivo con un asesor especializado."
    )


def format_support_manual_checks_reply(issue_type: str, contract: dict) -> str:
    """Da formato a support manual checks reply para presentarlo de forma clara."""
    code = contract_code(contract)
    intros = {
        "no_service": (
            f"Antes de seguir con la revisión del contrato *{code}*, reinicie manualmente la ONU y el router: "
            "desconéctelos de la energía por unos segundos y vuelva a conectarlos. "
            "Asegúrese de que ambos queden encendidos, revise el cable de fibra y el cable de red, "
            "y si le es posible pruebe los equipos en otro tomacorriente."
        ),
        "intermittence": (
            f"Antes de seguir con la revisión del contrato *{code}*, reinicie manualmente la ONU y el router: "
            "desconéctelos de la energía por unos segundos y vuelva a conectarlos. "
            "Después pruebe los equipos en otro tomacorriente "
            "y verifique si la intermitencia ocurre en todos los dispositivos o solo en uno."
        ),
        "slow_internet": (
            f"Antes de seguir con la revisión del contrato *{code}*, reinicie manualmente la ONU y el router: "
            "desconéctelos de la energía por unos segundos y vuelva a conectarlos. "
            "Luego pruebe cerca del router, asegúrese de estar conectado a la red correcta "
            "y valide si la lentitud ocurre en todos los dispositivos o solo en uno."
        ),
        "generic_network": format_support_issue_triage_reply(contract),
    }
    final = " Cuando termine, indíqueme si *sigue igual* o coménteme si el problema es falta de servicio, intermitencia o lentitud."
    return intros.get(issue_type, intros["generic_network"]) + final
