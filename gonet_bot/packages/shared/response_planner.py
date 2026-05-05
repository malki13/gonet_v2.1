"""Construcción de planes de respuesta según la intención detectada."""

from typing import Any, Iterable

from packages.agents.contact_contract_utils import contract_code, contract_due_value, format_money
from packages.shared.schemas import ResponsePlan


def _clean_evidence(items: Iterable[str | None]) -> list[str]:
    """Limpia evidence."""
    evidence: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if not value:
            continue
        normalized = value.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        evidence.append(value)
    return evidence


def response_plan_metadata(plan: ResponsePlan | None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Empaqueta los metadatos del plan de respuesta."""
    payload = dict(metadata or {})
    if plan is not None:
        payload["response_plan"] = plan.model_dump(exclude_none=True)
    return payload


def build_support_response_plan(
    *,
    message: str,
    contract: dict | None,
    issue_type: str | None = None,
    conversation_state: str,
    diagnostic_context: dict[str, Any] | None = None,
    observations: dict[str, Any] | None = None,
    followup_prompt: str | None = None,
    reply_goal: str | None = None,
    should_handoff: bool = False,
    handoff_reason: str | None = None,
) -> ResponsePlan:
    """Construye plan de respuesta de soporte a partir del contexto disponible."""
    diagnostic_context = dict(diagnostic_context or {})
    observations = dict(observations or {})
    code = contract_code(contract or {})
    connected_devices = diagnostic_context.get("connected_devices")
    onu_status = diagnostic_context.get("onu_status")
    power_dbm = diagnostic_context.get("power_dbm")
    plan_name = diagnostic_context.get("plan_name")
    plan_speed_mbps = diagnostic_context.get("plan_speed_mbps")
    device_model = diagnostic_context.get("device_model")
    network_count = diagnostic_context.get("network_count")
    lan_devices = diagnostic_context.get("lan_devices")
    mesh_devices = diagnostic_context.get("mesh_devices")
    wifi_devices = diagnostic_context.get("wifi_devices")
    wifi_24g_devices = diagnostic_context.get("wifi_24g_devices")
    wifi_5g_devices = diagnostic_context.get("wifi_5g_devices")
    evidence = _clean_evidence(
        [
            f"Contrato {code}" if code else None,
            f"Tipo de inconveniente: {issue_type}" if issue_type else None,
            f"Equipos conectados: {connected_devices}" if connected_devices is not None else None,
            f"Estado ONU: {onu_status}" if onu_status else None,
            f"Potencia ONU: {power_dbm} dBm" if power_dbm is not None else None,
            f"Plan reportado por CPE: {plan_name}" if plan_name else None,
            f"Velocidad reportada por CPE: {plan_speed_mbps} Mbps" if plan_speed_mbps is not None else None,
            f"Modelo CPE: {device_model}" if device_model else None,
            f"Redes principales: {network_count}" if network_count is not None else None,
            f"Dispositivos LAN: {lan_devices}" if lan_devices is not None else None,
            f"Dispositivos Mesh: {mesh_devices}" if mesh_devices is not None else None,
            f"Dispositivos WiFi: {wifi_devices}" if wifi_devices is not None else None,
            f"Dispositivos 2.4G: {wifi_24g_devices}" if wifi_24g_devices is not None else None,
            f"Dispositivos 5G: {wifi_5g_devices}" if wifi_5g_devices is not None else None,
            f"Ámbito reportado: {observations.get('device_scope')}" if observations.get("device_scope") else None,
            f"Medio afectado: {observations.get('connection_type')}" if observations.get("connection_type") else None,
            f"Prueba cerca del router: {observations.get('near_router_result')}" if observations.get("near_router_result") else None,
            f"Servicio afectado: {observations.get('affected_service')}" if observations.get("affected_service") else None,
        ]
    )
    return ResponsePlan(
        domain="support",
        conversation_state=conversation_state,
        message=str(message or "").strip(),
        reply_goal=reply_goal or ("escalar el caso técnico" if should_handoff else "guiar al cliente con troubleshooting útil"),
        hypothesis=str(diagnostic_context.get("hypothesis") or "").strip() or None,
        evidence=evidence,
        next_step=str(diagnostic_context.get("next_step") or "").strip() or None,
        followup_prompt=str(followup_prompt or diagnostic_context.get("followup_prompt") or "").strip() or None,
        should_handoff=should_handoff,
        handoff_reason=str(handoff_reason or "").strip() or None,
    )


def build_billing_response_plan(
    *,
    message: str,
    contract: dict | None,
    conversation_state: str,
    reply_goal: str,
    hypothesis: str | None = None,
    next_step: str | None = None,
    followup_prompt: str | None = None,
    should_handoff: bool = False,
    handoff_reason: str | None = None,
    proof_attempts: int | None = None,
    ocr_status: str | None = None,
    reconnect_status: str | None = None,
) -> ResponsePlan:
    """Construye plan de respuesta de facturacion a partir del contexto disponible."""
    code = contract_code(contract or {})
    pending_value = contract_due_value(contract or {})
    evidence = _clean_evidence(
        [
            f"Contrato {code}" if code else None,
            f"Valor pendiente: ${format_money(pending_value)}" if pending_value > 0 else None,
            f"Intento OCR: {proof_attempts}" if proof_attempts else None,
            f"Estado OCR: {ocr_status}" if ocr_status else None,
            f"Estado reconexión: {reconnect_status}" if reconnect_status else None,
        ]
    )
    return ResponsePlan(
        domain="billing",
        conversation_state=conversation_state,
        message=str(message or "").strip(),
        reply_goal=reply_goal,
        hypothesis=str(hypothesis or "").strip() or None,
        evidence=evidence,
        next_step=str(next_step or "").strip() or None,
        followup_prompt=str(followup_prompt or "").strip() or None,
        should_handoff=should_handoff,
        handoff_reason=str(handoff_reason or "").strip() or None,
    )


def build_sales_response_plan(
    *,
    message: str,
    conversation_state: str,
    reply_goal: str,
    profile: dict[str, Any] | None = None,
    recommended_plan: dict[str, Any] | None = None,
    next_step: str | None = None,
    followup_prompt: str | None = None,
    hypothesis: str | None = None,
) -> ResponsePlan:
    """Construye plan de respuesta comercial a partir del contexto disponible."""
    profile = dict(profile or {})
    recommended_plan = dict(recommended_plan or {})
    evidence = _clean_evidence(
        [
            f"Segmento: {profile.get('segment')}" if profile.get("segment") else None,
            f"Personas: {profile.get('people')}" if profile.get("people") else None,
            f"Dispositivos: {profile.get('devices')}" if profile.get("devices") else None,
            f"Espacio: {profile.get('space_size')}" if profile.get("space_size") else None,
            f"Uso principal: {profile.get('usage')}" if profile.get("usage") else None,
            f"Plan recomendado: {recommended_plan.get('name')}" if recommended_plan.get("name") else None,
            f"Velocidad sugerida: {recommended_plan.get('mbps')} Mbps" if recommended_plan.get("mbps") else None,
            f"Precio sugerido: ${recommended_plan.get('price')}" if recommended_plan.get("price") not in (None, "") else None,
        ]
    )
    return ResponsePlan(
        domain="sales",
        conversation_state=conversation_state,
        message=str(message or "").strip(),
        reply_goal=reply_goal,
        hypothesis=str(hypothesis or "").strip() or None,
        evidence=evidence,
        next_step=str(next_step or "").strip() or None,
        followup_prompt=str(followup_prompt or "").strip() or None,
    )


def build_handoff_response_plan(
    *,
    message: str,
    conversation_state: str,
    reply_goal: str,
    summary: str | None = None,
    target_group: str | None = None,
    hypothesis: str | None = None,
    next_step: str | None = None,
    followup_prompt: str | None = None,
    should_handoff: bool = True,
    handoff_reason: str | None = None,
) -> ResponsePlan:
    """Construye plan handoff respuesta a partir del contexto disponible."""
    evidence = _clean_evidence(
        [
            f"Grupo destino: {target_group}" if target_group else None,
            str(summary or "").strip()[:240] if summary else None,
        ]
    )
    return ResponsePlan(
        domain="handoff",
        conversation_state=conversation_state,
        message=str(message or "").strip(),
        reply_goal=reply_goal,
        hypothesis=str(hypothesis or "").strip() or None,
        evidence=evidence,
        next_step=str(next_step or "").strip() or None,
        followup_prompt=str(followup_prompt or "").strip() or None,
        should_handoff=should_handoff,
        handoff_reason=str(handoff_reason or summary or "").strip() or None,
    )


def build_clarify_response_plan(
    *,
    message: str,
    conversation_state: str,
    reply_goal: str,
    hypothesis: str | None = None,
    next_step: str | None = None,
    followup_prompt: str | None = None,
    pending_agent: str | None = None,
    pending_message: str | None = None,
    evidence: Iterable[str | None] | None = None,
) -> ResponsePlan:
    """Construye plan clarify respuesta a partir del contexto disponible."""
    return ResponsePlan(
        domain="clarify",
        conversation_state=conversation_state,
        message=str(message or "").strip(),
        reply_goal=reply_goal,
        hypothesis=str(hypothesis or "").strip() or None,
        evidence=_clean_evidence(
            [
                f"Área pendiente: {pending_agent}" if pending_agent else None,
                f"Consulta pendiente: {pending_message}" if pending_message else None,
                *(list(evidence or [])),
            ]
        ),
        next_step=str(next_step or "").strip() or None,
        followup_prompt=str(followup_prompt or "").strip() or None,
    )
