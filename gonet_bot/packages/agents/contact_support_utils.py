"""Ayudas para interpretar y responder mensajes de soporte."""

import re
from typing import Any

from packages.agents.contact_contract_utils import contract_code, contract_display_name, contract_status_label
from packages.agents.support_copy import format_support_monitoring_reply, format_support_recovery_reply
from packages.shared.utils import contains_any_phrase, matches_any_phrase, normalize_text


def _normalize_support_text(text: str) -> str:
    """Normaliza texto support."""
    return normalize_text(text)


def is_support_greeting(text: str) -> bool:
    """Indica si saludo de soporte se cumple."""
    lowered = _normalize_support_text(text)
    return matches_any_phrase(lowered, {
        "hola",
        "buenas",
        "buenos dias",
        "buenas tardes",
        "buenas noches",
        "holi",
        "hello",
        "hi",
    })


def is_support_option_selection(text: str) -> bool:
    """Indica si seleccion de opcion de soporte se cumple."""
    lowered = (text or "").strip().lower()
    return lowered in {
        "1",
        "2",
        "3",
        "problemas red",
        "editar redes",
        "editar red",
        "asesor especializado",
        "agente especializado",
        "asistencia humana",
    }


def user_requests_human(text: str) -> bool:
    """Devuelve el human user requests."""
    if not text:
        return False
    lowered = _normalize_support_text(text)
    short_requests = {
        "asesor",
        "asesora",
        "un asesor",
        "una asesora",
        "asesor por favor",
        "asesora por favor",
        "humano",
        "humana",
        "persona",
        "agente",
        "operador",
    }
    if lowered in short_requests:
        return True
    explicit_phrases = {
        "asistencia humana",
        "asesor especializado",
        "agente especializado",
        "quiero hablar con un asesor",
        "quiero hablar con un humano",
        "quiero hablar con una persona",
        "quiero hablar con un agente",
        "quiero hablar con un operador",
        "necesito hablar con un asesor",
        "necesito hablar con un humano",
        "pasame con un asesor",
        "pasame con un humano",
        "pasame con una persona",
        "pasame con un agente",
        "pasame con un operador",
        "pasa con un asesor",
        "pasa con un humano",
        "comunicarme con un asesor",
        "comunicarme con un humano",
        "prefiero un asesor",
        "prefiero hablar con un asesor",
        "transferir con un asesor",
        "derivame con un asesor",
        "derivame con un humano",
    }
    if lowered in explicit_phrases:
        return True
    patterns = [
        r"\b(?:hola|buenas|buenos dias|buenas tardes|buenas noches|hey|hi|hello)\b.{0,24}\b(?:asesor|asesora|humano|humana|persona|agente|operador)\b",
        r"\b(?:asesor|asesora|humano|humana|persona|agente|operador)\b.{0,24}\b(?:por favor|plis|pls|de favor)\b",
        r"\b(?:quiero|quisiera|necesito|prefiero)\s+(?:hablar|comunicarme|contactarme|ser\s+atendido|atencion|atencion\s+humana|atencion\s+personalizada)\b.{0,20}\b(?:asesor|humano|persona|agente|operador)\b",
        r"\b(?:pasame|pasa|transfiereme|derivame|deriva|transferirme)\b.{0,20}\b(?:con\s+)?(?:un\s+|una\s+)?(?:asesor|humano|persona|agente|operador)\b",
        r"\b(?:hablar|comunicarme|contactarme)\s+(?:con\s+)?(?:un\s+|una\s+)?(?:asesor|humano|persona|agente|operador)\b",
        r"\b(?:asesor|agente)\s+especializado\b",
        r"\basistencia\s+humana\b",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def user_reports_missing_otp(text: str) -> bool:
    """Devuelve el otp user reports missing."""
    if not text:
        return False
    lowered = text.lower()
    patterns = [
        r"no\s+tengo\s+el\s+c[oó]digo",
        r"no\s+tengo\s+el\s+otp",
        r"no\s+me\s+lleg[oó]\s+el\s+c[oó]digo",
        r"no\s+me\s+llega\s+el\s+c[oó]digo",
        r"no\s+recib[ií]\s+el\s+c[oó]digo",
        r"no\s+recibo\s+el\s+c[oó]digo",
        r"no\s+s[eé]\s+el\s+c[oó]digo",
        r"no\s+lo\s+tengo",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def parse_edit_redes_payload(text: str) -> tuple[str, str]:
    """Devuelve el payload parse edit redes."""
    name = ""
    pwd = ""
    match_name = re.search(r"(?:nombre|ssid)\s*[:=]\s*([^\n,;]+)", text, re.IGNORECASE)
    if match_name:
        name = match_name.group(1).strip()
    match_pwd = re.search(r"(?:password|contraseña|clave)\s*[:=]\s*([^\n,;]+)", text, re.IGNORECASE)
    if match_pwd:
        pwd = match_pwd.group(1).strip()
    return name, pwd


def user_requests_current_network_names(text: str) -> bool:
    """Devuelve los names user requests current network."""
    lowered = _normalize_support_text(text)
    if not lowered:
        return False
    patterns = [
        "como se llaman",
        "como se llama",
        "como se llaman mis redes",
        "como se llaman las redes",
        "mostrar mis redes",
        "muestrame mis redes",
        "mostrarme mis redes",
        "ver mis redes",
        "cuales son",
        "que nombres tienen",
        "redes actuales",
        "nombres actuales",
        "nombre actual de mis redes",
        "como estan mis redes",
        "que nombres tienen mis redes",
        "cuales son mis redes actuales",
    ]
    return contains_any_phrase(lowered, patterns)


def classify_support_issue(text: str) -> str:
    """Clasifica el problema de soporte."""
    lowered = _normalize_support_text(text)
    if matches_any_phrase(lowered, {"3", "asistencia humana", "asesor especializado", "agente especializado"}) or user_requests_human(lowered):
        return "human"
    if contains_any_phrase(
        lowered,
        (
            "red temporal",
            "redes temporales",
            "mostrar redes",
            "ver redes disponibles",
            "activar red",
            "desactivar red",
            "habilitar red",
            "apagar red",
            "encender red",
        ),
    ):
        return "human"
    edit_patterns = [
        "editar redes",
        "editar red",
        "cambiar clave",
        "cambiar la clave",
        "cambiar contrasena",
        "cambiar contraseña",
        "cambiar la contrasena",
        "cambiar la contraseña",
        "cambiar password",
        "cambiar nombre",
        "nombre wifi",
        "nombre de red",
        "clave wifi",
        "contrasena wifi",
        "contraseña wifi",
        "contrasena del wifi",
        "contraseña del wifi",
        "credenciales wifi",
        "credenciales de red",
    ]
    if matches_any_phrase(lowered, {"2"}) or contains_any_phrase(lowered, edit_patterns):
        return "edit_network"
    if contains_any_phrase(lowered, ("cambiar",)) and contains_any_phrase(lowered, ("wifi", "red")) and contains_any_phrase(
        lowered,
        ("contrasena", "contraseña", "clave", "nombre"),
    ):
        return "edit_network"
    no_service_patterns = [
        "sin servicio",
        "sin internet",
        "no tengo internet",
        "no funciona internet",
        "internet no funciona",
        "internet no vale",
        "mi internet no vale",
        "internet no sirve",
        "no sirve el internet",
        "el internet no sirve",
        "no tengo servicio",
        "no navega",
        "no hay internet",
        "se fue el internet",
        "no conecta",
        "sin senal",
        "sin señal",
    ]
    if contains_any_phrase(lowered, no_service_patterns):
        return "no_service"
    if contains_any_phrase(lowered, ("no sirve", "no funciona", "no vale")) and contains_any_phrase(
        lowered,
        ("internet", "wifi", "red", "servicio"),
    ):
        return "no_service"
    if re.search(
        r"\bya\s+revis(?:e|é)\b.{0,24}\b(?:no\s+sirve|no\s+vale|no\s+funciona|sigue\s+igual|sigue\s+sin\s+internet)\b",
        lowered,
    ):
        return "no_service"
    if re.search(
        r"\bya\s+prob(?:e|é)\b.{0,24}\b(?:no\s+sirve|no\s+vale|no\s+funciona|sigue\s+igual|sigue\s+sin\s+internet)\b",
        lowered,
    ):
        return "no_service"
    intermittent_patterns = [
        "intermiten",
        "se va",
        "se cae",
        "a ratos",
        "por ratos",
        "por momentos",
        "va y viene",
        "se corta",
        "se corta a ratos",
        "inestable",
        "cortes",
        "se desconecta",
    ]
    if contains_any_phrase(lowered, intermittent_patterns):
        return "intermittence"
    slow_patterns = [
        "internet lento",
        "muy lento",
        "lentitud",
        "lento",
        "esta lento",
        "esta muy lento",
        "anda lento",
        "va lento",
        "se pone lento",
        "baja velocidad",
        "va despacio",
    ]
    if contains_any_phrase(lowered, slow_patterns):
        return "slow_internet"
    if matches_any_phrase(lowered, {"1", "problemas red"}):
        return "generic_network"
    generic_patterns = [
        "problema con la red",
        "problemas con la red",
        "problemas de red",
        "problema internet",
        "soporte tecnico",
        "soporte técnico",
        "ayuda con internet",
        "ayuda con mi red",
        "wifi",
        "internet",
        "red",
    ]
    if contains_any_phrase(lowered, generic_patterns):
        return "generic_network"
    return "unknown"


def is_affirmative(text: str) -> bool:
    """Indica si affirmative se cumple."""
    lowered = _normalize_support_text(text)
    patterns = [
        r"^si$",
        r"^sip$",
        r"^s+i+$",
        r"^yes$",
        r"\bse solucion[oó]\b",
        r"\bya funciona\b",
        r"\bfunciona bien\b",
        r"\bqued[oó] bien\b",
        r"\bya qued[oó]\b",
        r"\bya est[aá] bien\b",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def is_acknowledgement(text: str) -> bool:
    """Indica si acknowledgement se cumple."""
    lowered = _normalize_support_text(text)
    if not lowered:
        return False
    return lowered in {
        "ok",
        "okay",
        "dale",
        "de una",
        "va",
        "listo",
        "perfecto",
        "entendido",
        "comprendo",
        "ya",
    }


def is_negative(text: str) -> bool:
    """Indica si negative se cumple."""
    lowered = _normalize_support_text(text)
    patterns = [
        r"^no$",
        r"\bsigue lento\b",
        r"\besta lento\b",
        r"\bestá lento\b",
        r"\bsigue intermitente\b",
        r"\bsigue sin internet\b",
        r"\btodavia sigue\b",
        r"\btodavia continua\b",
        r"\baun sigue\b",
        r"\baun continua\b",
        r"\bsigue igual\b",
        r"\bcontinua igual\b",
        r"\bcontin[uú]a igual\b",
        r"\bpersiste\b",
        r"\bsigue fallando\b",
        r"\bno se solucion[oó]\b",
        r"\bno funciona\b",
        r"\bno sirve\b",
        r"\bno vale\b",
        r"\bya\s+revis(?:e|é)\b.{0,24}\b(?:no\s+sirve|no\s+vale|no\s+funciona|sigue\s+igual|sigue\s+sin\s+internet)\b",
        r"\bya\s+prob(?:e|é)\b.{0,24}\b(?:no\s+sirve|no\s+vale|no\s+funciona|sigue\s+igual|sigue\s+sin\s+internet)\b",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def user_reports_manual_checks_done(text: str) -> bool:
    """Indica si el cliente ya realizó las validaciones manuales sugeridas."""
    lowered = _normalize_support_text(text)
    if not lowered:
        return False
    patterns = [
        r"\bya\s+hice\s+eso\b",
        r"\bya\s+lo\s+hice\b",
        r"\bya\s+lo\s+reinic[ii][eé]\b",
        r"\bya\s+reinici[eé]\s+la?\s*onu\b",
        r"\bya\s+reinici[eé]\s+el\s+router\b",
        r"\bya\s+desconect[eé]\s+la?\s*onu\b",
        r"\bya\s+desconect[eé]\s+el\s+router\b",
        r"\bya\s+prob[eé]\b",
        r"\bya\s+revis[eé]\b",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def extract_support_followup_observations(text: str) -> dict[str, str | bool]:
    """Extrae support followup observations."""
    lowered = _normalize_support_text(text)
    if not lowered:
        return {}

    observations: dict[str, str | bool] = {}

    all_devices_patterns = (
        "en todos",
        "todos los dispositivos",
        "todos los equipos",
        "en toda la casa",
        "en todo",
    )
    single_device_patterns = (
        "solo en uno",
        "solo en un equipo",
        "solo en un dispositivo",
        "solo en mi celular",
        "solo en el celular",
        "solo en mi telefono",
        "solo en el telefono",
        "solo en la tv",
        "solo en el televisor",
        "solo en el tele",
        "solo en la laptop",
        "solo en la computadora",
        "solo en la pc",
    )
    if contains_any_phrase(lowered, all_devices_patterns):
        observations["device_scope"] = "all_devices"
    elif contains_any_phrase(lowered, single_device_patterns):
        observations["device_scope"] = "single_device"

    if contains_any_phrase(lowered, ("por wifi", "solo wifi", "solo por wifi", "inalambrico", "inalambrica", "inalámbrica")):
        observations["connection_type"] = "wifi"
    elif contains_any_phrase(lowered, ("por cable", "por ethernet", "cable ethernet", "cableado", "lan")):
        observations["connection_type"] = "ethernet"
    elif contains_any_phrase(lowered, ("wifi y cable", "por wifi y por cable", "en wifi y cable")):
        observations["connection_type"] = "both"

    near_router_patterns = (
        "cerca del router",
        "junto al router",
        "al lado del router",
        "pegado al router",
        "a lado del router",
        "cerca de router",
    )
    if contains_any_phrase(lowered, near_router_patterns):
        if contains_any_phrase(lowered, ("mejora", "mejor", "va bien", "ahi si", "ahí sí", "me va mejor", "si mejora")):
            observations["near_router_result"] = "better"
        elif contains_any_phrase(lowered, ("igual", "sigue lento", "no mejora", "continua igual", "continúa igual", "sigue igual")):
            observations["near_router_result"] = "same"
        else:
            observations["tested_near_router"] = True

    if contains_any_phrase(lowered, ("streaming", "netflix", "youtube", "roku", "iptv", "peliculas", "películas", "series")):
        observations["affected_service"] = "streaming"
    elif contains_any_phrase(lowered, ("juegos", "gaming", "xbox", "playstation", "ps5", "ps4", "fortnite")):
        observations["affected_service"] = "gaming"
    elif contains_any_phrase(lowered, ("zoom", "meet", "teams", "videollamadas", "videollamada", "llamadas")):
        observations["affected_service"] = "videocalls"
    elif contains_any_phrase(lowered, ("navegacion", "navegación", "paginas", "páginas", "general")):
        observations["affected_service"] = "general_browsing"

    return observations
def build_support_diagnostic_context(
    *,
    issue_type: str,
    contract: dict,
    connected_devices: int | None,
    onu_status: str | None,
    power_dbm: float | None,
    rebooted: bool,
    proactive_reboot: bool,
    smart_snapshot: dict[str, Any] | None = None,
    connected_device_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construye contexto diagnostico de soporte a partir del contexto disponible."""
    code = contract_code(contract)
    smart_snapshot = dict(smart_snapshot or {})
    connected_device_summary = dict(connected_device_summary or {})
    plan_name = str(smart_snapshot.get("plan_name") or "").strip() or None
    plan_speed_mbps = smart_snapshot.get("plan_speed_mbps")
    device_model = str(smart_snapshot.get("device_model") or "").strip() or None
    network_count = smart_snapshot.get("network_count")
    device_id = str(smart_snapshot.get("device_id") or "").strip() or None
    lan_devices = connected_device_summary.get("lan_devices")
    mesh_devices = connected_device_summary.get("mesh_devices")
    wifi_devices = connected_device_summary.get("wifi_devices")
    wifi_24g_devices = connected_device_summary.get("wifi_24g_devices")
    wifi_5g_devices = connected_device_summary.get("wifi_5g_devices")
    context: dict[str, Any] = {
        "issue_type": issue_type,
        "contract_code": code,
        "connected_devices": connected_devices,
        "onu_status": onu_status,
        "power_dbm": power_dbm,
        "rebooted": rebooted,
        "proactive_reboot": proactive_reboot,
        "message": "",
        "followup_prompt": "",
        "diagnostic_style": "generic",
        "hypothesis": "",
        "next_step": "",
        "device_id": device_id,
        "plan_name": plan_name,
        "plan_speed_mbps": plan_speed_mbps,
        "device_model": device_model,
        "network_count": network_count,
        "lan_devices": lan_devices,
        "mesh_devices": mesh_devices,
        "wifi_devices": wifi_devices,
        "wifi_24g_devices": wifi_24g_devices,
        "wifi_5g_devices": wifi_5g_devices,
        "network_counts": connected_device_summary.get("network_counts") or {},
    }

    if rebooted:
        context["message"] = format_support_recovery_reply(contract, proactive=proactive_reboot)
        context["followup_prompt"] = "Cuando termine de estabilizarse, indíqueme si ya quedó bien o si todavía sigue igual."
        context["diagnostic_style"] = "recovery"
        context["hypothesis"] = "recovery_in_progress"
        context["next_step"] = "wait_and_confirm"
        return context

    if issue_type == "slow_internet":
        if connected_devices is not None and connected_devices >= 8:
            context["message"] = (
                f"Ya revisé su contrato *{code}* y la señal base se ve estable. "
                f"Ahora mismo veo *{connected_devices}* equipos conectados a su red, así que es muy posible que la lentitud venga por carga en el wifi.\n\n"
                "Hagamos una prueba rápida: deje un solo equipo conectado cerca del router, pause por un momento TVs, streaming o descargas pesadas "
                "e indíqueme si así mejora o si sigue lento."
            )
            context["followup_prompt"] = (
                "Indíqueme si así mejora, si la lentitud ocurre en todos o solo en uno, o si solo pasa por wifi."
            )
            context["diagnostic_style"] = "wifi_load_high"
            context["hypothesis"] = "wifi_load"
            context["next_step"] = "reduce_load_and_isolate_device"
            return context
        if connected_devices is not None and connected_devices >= 4:
            context["message"] = (
                f"Ya revisé su contrato *{code}* y la señal base se ve estable. "
                f"Veo *{connected_devices}* equipos conectados, así que primero quiero descartar carga en la red.\n\n"
                "Pruebe cerca del router con un solo equipo e indíqueme si la lentitud mejora ahí o si se mantiene igual."
            )
            context["followup_prompt"] = "Si le es posible, indíqueme también si ocurre en todos los equipos o solo en uno."
            context["diagnostic_style"] = "wifi_load_medium"
            context["hypothesis"] = "wifi_load_possible"
            context["next_step"] = "near_router_single_device_test"
            return context
        context["message"] = (
            f"Ya revisé su contrato *{code}* y no veo una falla base en la línea. "
            "Como no aparecen muchos equipos conectados, quiero ubicar si la lentitud es general o puntual.\n\n"
            "Indíqueme si ocurre en todos los dispositivos o solo en uno y, si le es posible, haga la prueba cerca del router."
        )
        context["followup_prompt"] = "También me sirve saber si ocurre solo por wifi o también por cable."
        context["diagnostic_style"] = "slow_point_check"
        context["hypothesis"] = "device_or_wifi_specific"
        context["next_step"] = "scope_and_connection_type_check"
        return context

    if issue_type == "no_service":
        signal_note = "La ONU se ve estable." if onu_status == "working" else "No pude confirmar una alerta crítica en la ONU."
        context["message"] = (
            f"Ya revisé su contrato *{code}*. {signal_note} "
            "Quiero descartar primero algo físico en casa: revise que la ONU y el router estén encendidos y que los cables estén bien conectados."
        )
        context["followup_prompt"] = "Si ya lo revisó y sigue sin internet, indíqueme y lo derivo con un asesor especializado."
        context["diagnostic_style"] = "no_service_check"
        context["hypothesis"] = "physical_check_pending"
        context["next_step"] = "confirm_power_and_cabling"
        return context

    if issue_type == "intermittence":
        context["message"] = (
            f"Ya revisé su contrato *{code}* y no veo una caída total de la línea. "
            "Para ubicar la causa, reinicie la ONU y el router e indíqueme si la intermitencia ocurre en todos los equipos o solo en uno."
        )
        context["followup_prompt"] = "Si le es posible, indíqueme también si ocurre por wifi, por cable o en ambos."
        context["diagnostic_style"] = "intermittence_check"
        context["hypothesis"] = "wifi_or_local_instability"
        context["next_step"] = "scope_and_medium_check"
        return context

    context["message"] = format_support_monitoring_reply(issue_type, contract)
    context["followup_prompt"] = "Si después de eso sigue igual, avíseme y lo derivo con un asesor especializado."
    context["hypothesis"] = "generic_followup"
    context["next_step"] = "confirm_persistence"
    return context


def build_support_followup_plan(
    *,
    contract: dict,
    diagnostic_context: dict | None,
    observations: dict[str, str | bool],
    attempts: int = 0,
    user_reports_persisting: bool = False,
) -> dict[str, str | bool]:
    """Construye plan de seguimiento de soporte a partir del contexto disponible."""
    code = contract_code(contract)
    diagnostic_context = diagnostic_context or {}
    issue_type = str(diagnostic_context.get("issue_type") or "")
    connected_devices = diagnostic_context.get("connected_devices")
    plan: dict[str, str | bool] = {
        "message": "",
        "followup_prompt": "",
        "should_handoff": False,
        "handoff_reason": "",
        "hypothesis": "",
        "next_step": "",
    }
    if issue_type != "slow_internet":
        return plan

    if observations.get("near_router_result") == "better":
        plan["message"] = (
            f"Eso me sirve bastante. Si mejora cerca del router en el contrato *{code}*, lo más probable es que el problema sea de cobertura wifi y no de la línea.\n\n"
            "Prueba usar la red 5G si estás cerca, deja el router en un lugar más abierto y evita obstáculos grandes. "
            "Indíqueme en qué parte de la casa se pone lento y continúo guiándole."
        )
        plan["followup_prompt"] = "Indíqueme en qué zona de la casa se pone lento o, si lo prefiere, lo derivo con un asesor especializado."
        plan["hypothesis"] = "wifi_coverage"
        plan["next_step"] = "optimize_wifi_placement"
        return plan

    if observations.get("device_scope") == "single_device":
        plan["message"] = (
            f"Perfecto, entonces la lentitud parece concentrarse en un solo equipo del contrato *{code}*.\n\n"
            "En ese caso, pruebe reiniciar ese dispositivo, olvidar y reconectar la red wifi y, si le es posible, probarlo en 5G o por cable. "
            "Si en los demás equipos navega bien, no parece una caída general del servicio."
        )
        plan["followup_prompt"] = "Después de esa prueba, indíqueme si ese equipo mejora o si sigue lento solo ahí."
        plan["hypothesis"] = "single_device_issue"
        plan["next_step"] = "restart_and_reconnect_single_device"
        return plan

    if observations.get("connection_type") == "wifi":
        plan["message"] = (
            f"Eso apunta más a un tema de wifi que de línea en el contrato *{code}*.\n\n"
            "Haga la prueba cerca del router, use la red 5G si está próximo al equipo y revise si mejora. "
            "Si sigue igual incluso cerca del router, lo derivo con un asesor especializado."
        )
        plan["followup_prompt"] = "Indíqueme si mejora cerca del router o si el problema sigue igual incluso ahí."
        plan["hypothesis"] = "wifi_specific_issue"
        plan["next_step"] = "near_router_wifi_test"
        return plan

    if not observations:
        if user_reports_persisting and attempts >= 1:
            plan["should_handoff"] = True
            plan["handoff_reason"] = (
                f"La lentitud del contrato {code} persiste y aún no se pudo aislar si afecta a uno o varios equipos."
            )
            plan["hypothesis"] = "unisolated_slow_internet"
            plan["next_step"] = "handoff_specialist"
            return plan
        plan["message"] = (
            f"Para ubicar mejor la causa en el contrato *{code}*, solo necesito una precisión más.\n\n"
            "Indíqueme si la lentitud ocurre en todos los equipos o solo en uno y, si le es posible, si solo pasa por wifi o también por cable."
        )
        plan["followup_prompt"] = "Con esa respuesta seguimos sin hacerle repetir todo."
        plan["hypothesis"] = "needs_scope_detail"
        plan["next_step"] = "collect_scope_detail"
        return plan

    if observations.get("device_scope") == "all_devices":
        if observations.get("near_router_result") == "same" or observations.get("connection_type") in {"both", "ethernet"}:
            if user_reports_persisting and attempts >= 1:
                plan["should_handoff"] = True
                plan["handoff_reason"] = (
                    f"La lentitud del contrato {code} afecta a varios equipos y no mejora con pruebas cercanas al router."
                )
                plan["hypothesis"] = "generalized_slow_internet"
                plan["next_step"] = "handoff_specialist"
                return plan
            plan["message"] = (
                f"Si ocurre en varios equipos del contrato *{code}* y no mejora ni cerca del router, entonces ya no parece algo puntual de wifi o de un solo dispositivo.\n\n"
                "Haga una última prueba con un solo equipo, preferiblemente por cable si le es posible, e indíqueme si también va lento ahí."
            )
            plan["followup_prompt"] = "Si incluso así sigue lento, lo derivo con un asesor especializado para una revisión más a fondo."
            plan["hypothesis"] = "generalized_slow_internet"
            plan["next_step"] = "single_device_ethernet_test"
            return plan
        if isinstance(connected_devices, int) and connected_devices >= 8:
            plan["message"] = (
                f"Si la lentitud pasa en todos y además veo *{connected_devices}* equipos conectados en el contrato *{code}*, primero quiero descartar saturación de red.\n\n"
                "Deje solo un equipo conectado cerca del router, pause por un momento streaming, TVs o descargas pesadas e indíqueme si así mejora."
            )
            plan["followup_prompt"] = "Si ni así cambia, lo derivo con un asesor especializado."
            plan["hypothesis"] = "wifi_load_high"
            plan["next_step"] = "reduce_concurrent_usage"
            return plan
        plan["message"] = (
            f"Si ocurre en todos los equipos del contrato *{code}*, entonces ya no parece algo puntual de un dispositivo.\n\n"
            "Haga una prueba cerca del router e indíqueme si también ocurre ahí. Si sigue igual, lo derivo con un asesor especializado para una revisión más a fondo."
        )
        plan["followup_prompt"] = "Si le es posible, indíqueme también si esto ocurre solo por wifi o también por cable."
        plan["hypothesis"] = "multi_device_issue"
        plan["next_step"] = "near_router_scope_check"
        return plan

    if observations.get("near_router_result") == "same":
        if user_reports_persisting and attempts >= 1:
            plan["should_handoff"] = True
            plan["handoff_reason"] = (
                f"La lentitud del contrato {code} persiste incluso cerca del router después de pruebas guiadas."
            )
            plan["hypothesis"] = "line_or_router_issue"
            plan["next_step"] = "handoff_specialist"
            return plan
        plan["message"] = (
            f"Si incluso cerca del router sigue lento en el contrato *{code}*, entonces no parece solo cobertura wifi.\n\n"
            "Indíqueme si esto ocurre en todos los equipos o solo en uno. Con eso le indico el siguiente paso."
        )
        plan["followup_prompt"] = "Si le es posible, indíqueme también si ocurre solo por wifi o también por cable."
        plan["hypothesis"] = "line_or_router_issue"
        plan["next_step"] = "scope_and_medium_check"
        return plan

    return plan


def format_support_followup_guidance(
    *,
    contract: dict,
    diagnostic_context: dict | None,
    observations: dict[str, str | bool],
) -> str:
    """Da formato a la guia de seguimiento de soporte para presentarlo de forma clara."""
    plan = build_support_followup_plan(
        contract=contract,
        diagnostic_context=diagnostic_context,
        observations=observations,
    )
    return str(plan.get("message") or "")


def _format_support_observations(observations: dict[str, str | bool] | None) -> list[str]:
    """Da formato a support observations para presentarlo de forma clara."""
    observations = dict(observations or {})
    lines: list[str] = []
    device_scope = observations.get("device_scope")
    if device_scope == "all_devices":
        lines.append("- Alcance reportado: afecta a varios equipos")
    elif device_scope == "single_device":
        lines.append("- Alcance reportado: afecta a un solo equipo")

    connection_type = observations.get("connection_type")
    if connection_type == "wifi":
        lines.append("- Medio reportado: solo wifi")
    elif connection_type == "ethernet":
        lines.append("- Medio reportado: solo cable")
    elif connection_type == "both":
        lines.append("- Medio reportado: wifi y cable")

    near_router_result = observations.get("near_router_result")
    if near_router_result == "better":
        lines.append("- Prueba cerca del router: mejora")
    elif near_router_result == "same":
        lines.append("- Prueba cerca del router: sigue igual")
    elif observations.get("tested_near_router") is True:
        lines.append("- Prueba cerca del router: realizada sin resultado concluyente")

    affected_service = observations.get("affected_service")
    service_labels = {
        "streaming": "streaming",
        "gaming": "gaming",
        "videocalls": "videollamadas",
        "general_browsing": "navegación general",
    }
    if affected_service in service_labels:
        lines.append(f"- Servicio afectado: {service_labels[affected_service]}")
    return lines


def _humanize_onu_status(value: str | None) -> str | None:
    """Humaniza el estado ONU para resúmenes de handoff."""
    normalized = str(value or "").strip().lower()
    labels = {
        "working": "operativa",
        "up": "operativa",
        "ok": "operativa",
        "los": "con alerta LOS",
        "dyinggasp": "con alerta de apagado repentino",
    }
    return labels.get(normalized)


def _humanize_support_hypothesis(value: str | None) -> str | None:
    """Traduce hipótesis internas a texto legible para Odoo."""
    normalized = str(value or "").strip().lower()
    labels = {
        "recovery_in_progress": "reinicio remoto en curso; pendiente confirmar estabilidad",
        "wifi_load": "posible saturación de la red wifi por varios equipos conectados",
        "wifi_load_possible": "posible carga alta en la red wifi",
        "device_or_wifi_specific": "el inconveniente podría estar concentrado en un dispositivo o en la conexión wifi",
        "physical_check_pending": "primero conviene descartar una causa física en casa",
        "wifi_or_local_instability": "la intermitencia podría estar asociada al wifi o a la red local",
        "generic_followup": "se requiere confirmar si el inconveniente persiste",
        "wifi_coverage": "posible problema de cobertura wifi",
        "single_device_issue": "el inconveniente parece concentrarse en un solo equipo",
        "wifi_specific_issue": "el inconveniente parece concentrarse en la conexión wifi",
        "unisolated_slow_internet": "la lentitud persiste y todavía no se ha aislado la causa",
        "needs_scope_detail": "falta confirmar el alcance exacto del inconveniente",
        "generalized_slow_internet": "la lentitud parece afectar a varios equipos",
        "multi_device_issue": "el inconveniente parece afectar a varios equipos",
        "line_or_router_issue": "el inconveniente podría estar en el router o en la línea",
    }
    return labels.get(normalized)


def _humanize_support_next_step(value: str | None) -> str | None:
    """Traduce el siguiente paso interno a texto legible para Odoo."""
    normalized = str(value or "").strip().lower()
    labels = {
        "wait_and_confirm": "esperar estabilización del servicio y confirmar resultado con el cliente",
        "reduce_load_and_isolate_device": "reducir carga de la red y aislar un equipo para prueba",
        "near_router_single_device_test": "hacer prueba cerca del router con un solo equipo",
        "scope_and_connection_type_check": "confirmar si afecta a todos los equipos y si ocurre por wifi o cable",
        "confirm_power_and_cabling": "confirmar energía y cableado de ONU y router",
        "scope_and_medium_check": "confirmar alcance del inconveniente y medio afectado",
        "confirm_persistence": "confirmar si el inconveniente persiste",
        "optimize_wifi_placement": "optimizar ubicación del router y cobertura wifi",
        "restart_and_reconnect_single_device": "reiniciar y reconectar el equipo afectado",
        "near_router_wifi_test": "validar comportamiento del wifi cerca del router",
        "handoff_specialist": "continuar revisión con asesor especializado",
        "collect_scope_detail": "recoger más detalle sobre el alcance del inconveniente",
        "single_device_ethernet_test": "hacer prueba con un solo equipo por cable",
        "reduce_concurrent_usage": "reducir uso simultáneo y repetir prueba",
        "near_router_scope_check": "validar alcance del problema con prueba cerca del router",
    }
    return labels.get(normalized)


def format_support_handoff_summary(
    *,
    reason: str,
    contract: dict,
    issue_type: str | None = None,
    diagnostic_context: dict[str, Any] | None = None,
    observations: dict[str, str | bool] | None = None,
    system_detail: str | None = None,
) -> str:
    """Da formato a el resumen para derivar soporte para presentarlo de forma clara."""
    diagnostic_context = dict(diagnostic_context or {})
    issue_value = str(issue_type or diagnostic_context.get("issue_type") or "").strip()
    lines = [
        "Caso: Servicio técnico",
        f"Motivo de escalamiento: {str(reason or '').strip()}",
        "",
        "Cliente / contrato:",
    ]

    holder_name = contract_display_name(contract or {})
    if holder_name:
        lines.append(f"- Titular: {holder_name}")
    code = contract_code(contract or {})
    if code:
        lines.append(f"- Contrato: {code}")
    if contract:
        lines.append(f"- Estado contrato: {contract_status_label(contract)}")
    if issue_value:
        lines.append(f"- Tipo de inconveniente: {support_issue_label(issue_value)}")

    diagnostic_lines: list[str] = []
    onu_status = _humanize_onu_status(diagnostic_context.get("onu_status"))
    if onu_status:
        diagnostic_lines.append(f"- Estado ONU: {onu_status}")
    power_dbm = diagnostic_context.get("power_dbm")
    if power_dbm is not None:
        diagnostic_lines.append(f"- Potencia ONU: {power_dbm} dBm")
    connected_devices = diagnostic_context.get("connected_devices")
    if connected_devices is not None:
        diagnostic_lines.append(f"- Equipos conectados: {connected_devices}")
    plan_name = str(diagnostic_context.get("plan_name") or "").strip()
    if plan_name:
        diagnostic_lines.append(f"- Plan reportado por CPE: {plan_name}")
    plan_speed_mbps = diagnostic_context.get("plan_speed_mbps")
    if plan_speed_mbps is not None:
        diagnostic_lines.append(f"- Velocidad reportada por CPE: {plan_speed_mbps} Mbps")
    device_model = str(diagnostic_context.get("device_model") or "").strip()
    if device_model:
        diagnostic_lines.append(f"- Modelo CPE: {device_model}")
    network_count = diagnostic_context.get("network_count")
    if network_count is not None:
        diagnostic_lines.append(f"- Redes principales detectadas: {network_count}")
    lan_devices = diagnostic_context.get("lan_devices")
    if lan_devices is not None:
        diagnostic_lines.append(f"- Dispositivos LAN: {lan_devices}")
    mesh_devices = diagnostic_context.get("mesh_devices")
    if mesh_devices is not None:
        diagnostic_lines.append(f"- Dispositivos Mesh: {mesh_devices}")
    wifi_devices = diagnostic_context.get("wifi_devices")
    if wifi_devices is not None:
        diagnostic_lines.append(f"- Dispositivos WiFi: {wifi_devices}")
    wifi_24g_devices = diagnostic_context.get("wifi_24g_devices")
    if wifi_24g_devices is not None:
        diagnostic_lines.append(f"- Dispositivos 2.4G: {wifi_24g_devices}")
    wifi_5g_devices = diagnostic_context.get("wifi_5g_devices")
    if wifi_5g_devices is not None:
        diagnostic_lines.append(f"- Dispositivos 5G: {wifi_5g_devices}")
    if diagnostic_context.get("rebooted") is True:
        action = "reinicio remoto de ONU y router"
        if diagnostic_context.get("proactive_reboot") is True:
            action = "reinicio remoto preventivo de ONU y router"
        diagnostic_lines.append(f"- Acción remota ejecutada: {action}")
    hypothesis = _humanize_support_hypothesis(diagnostic_context.get("hypothesis"))
    if hypothesis:
        diagnostic_lines.append(f"- Hipótesis actual: {hypothesis}")
    next_step = _humanize_support_next_step(diagnostic_context.get("next_step"))
    if next_step:
        diagnostic_lines.append(f"- Siguiente paso sugerido: {next_step}")
    if diagnostic_lines:
        lines.extend(["", "Diagnóstico disponible:", *diagnostic_lines])

    observation_lines = _format_support_observations(observations)
    if observation_lines:
        lines.extend(["", "Observaciones del cliente:", *observation_lines])

    detail = str(system_detail or "").strip()
    if detail:
        lines.extend(["", "Detalle técnico previo:", f"- {detail}"])

    return "\n".join(line for line in lines if line is not None).strip()


def support_issue_label(issue_type: str) -> str:
    """Devuelve la etiqueta del problema de soporte."""
    labels = {
        "no_service": "sin servicio",
        "intermittence": "intermitencias",
        "slow_internet": "internet lento",
        "generic_network": "problemas de red",
        "edit_network": "editar credenciales de red",
    }
    return labels.get(issue_type, issue_type or "soporte")
