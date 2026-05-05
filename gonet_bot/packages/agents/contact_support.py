"""Subflujo de contacto centrado en soporte técnico y traspaso guiado."""

import logging

import httpx

from packages.agents.support_copy import (
    format_support_clarification,
    format_support_device_count_reply,
    format_support_guided_handoff_reply,
    format_support_issue_nudge,
    format_support_issue_triage_reply,
    format_support_manual_checks_reply,
    format_support_monitoring_reply,
    format_support_recovery_reply,
)
from packages.agents.contact_support_utils import (
    build_support_followup_plan,
    build_support_diagnostic_context,
    classify_support_issue,
    extract_support_followup_observations,
    format_support_handoff_summary,
    is_acknowledgement,
    is_affirmative,
    is_negative,
    is_support_greeting,
    parse_edit_redes_payload,
    support_issue_label,
    user_reports_missing_otp,
    user_requests_current_network_names,
    user_reports_manual_checks_done,
)
from packages.shared.response_planner import build_support_response_plan, response_plan_metadata
from packages.shared.schemas import AgentResult, InboundMessage, SessionState

logger = logging.getLogger("contact_flow")
CONTACT_FLOW_EXTERNAL_ERRORS = (httpx.HTTPError, RuntimeError, ValueError)
EDIT_NETWORK_READY_PROMPT = (
    "Perfecto, ya validé el código. Ahora compártame el nuevo nombre de su wifi y la nueva contraseña.\n\n"
    "Para que sea más simple, envíeme solo un nombre base y yo me encargo del resto. "
    "Por ejemplo, si me comparte MiWifi, dejaré sus redes como MiWifi 2.4G y MiWifi 5G.\n\n"
    "Si le sirve, también puedo indicarle cómo se llaman hoy sus redes actuales.\n\n"
    "Puede enviármelo así:\n"
    "nombre: MiWifi\n"
    "contraseña: MiClave123"
)
EDIT_NETWORK_RETRY_PROMPT = (
    "Solo me falta el nuevo nombre de su wifi y la nueva contraseña para continuar.\n\n"
    "Envíeme un solo nombre base y yo configuraré las dos bandas por usted, por ejemplo: MiWifi 2.4G y MiWifi 5G.\n\n"
    "Envíemelo así:\n"
    "nombre: MiWifi\n"
    "contraseña: MiClave123"
)
EDIT_NETWORK_AFTER_LIST_PROMPT = (
    "Si desea cambiarlo, ahora envíeme el nuevo nombre base y la nueva contraseña.\n\n"
    "Ejemplo:\n"
    "nombre: MiWifi\n"
    "contraseña: MiClave123"
)
EDIT_NETWORK_LIST_FALLBACK_PROMPT = (
    "No pude consultar en este momento cómo se llaman sus redes actuales, "
    "pero igual podemos continuar con el cambio.\n\n"
    "Envíeme el nuevo nombre base y la nueva contraseña así:\n"
    "nombre: MiWifi\n"
    "contraseña: MiClave123"
)


class ContactSupportMixin:
    """Agrupa la lógica de soporte dentro del flujo de contacto."""
    
    @staticmethod
    def _clear_support_conversation_memory(support_state: dict) -> None:
        """Limpia memory support conversation."""
        support_state["last_diagnostic"] = None
        support_state["last_response_plan"] = None
        support_state["last_followup_prompt"] = None
        support_state["guided_followup_attempts"] = 0

    @staticmethod
    def _store_support_response_plan(support_state: dict, plan) -> dict:
        """Almacena el plan de respuesta de soporte."""
        dumped = plan.model_dump(exclude_none=True)
        support_state["last_response_plan"] = dumped
        support_state["last_followup_prompt"] = str(plan.followup_prompt or dumped.get("followup_prompt") or "")
        return dumped

    @staticmethod
    def _support_handoff_summary(
        *,
        contract: dict,
        issue_type: str | None,
        reason: str,
        diagnostic_context: dict | None = None,
        observations: dict | None = None,
        system_detail: str | None = None,
    ) -> str:
        """Resume el caso de soporte para un traspaso humano."""
        return format_support_handoff_summary(
            reason=reason,
            contract=contract,
            issue_type=issue_type,
            diagnostic_context=diagnostic_context,
            observations=observations,
            system_detail=system_detail,
        )

    def _support_response(
        self,
        *,
        support_state: dict,
        contract: dict,
        message: str,
        intent: str,
        conversation_state: str,
        issue_type: str | None = None,
        diagnostic_context: dict | None = None,
        observations: dict | None = None,
        followup_prompt: str | None = None,
        reply_goal: str | None = None,
        metadata: dict | None = None,
    ) -> AgentResult:
        """Devuelve la respuesta support."""
        response_plan = build_support_response_plan(
            message=message,
            contract=contract,
            issue_type=issue_type,
            conversation_state=conversation_state,
            diagnostic_context=diagnostic_context,
            observations=observations,
            followup_prompt=followup_prompt,
            reply_goal=reply_goal,
        )
        self._store_support_response_plan(support_state, response_plan)
        return AgentResult(
            message=str(response_plan.message),
            intent=intent,
            agent="support",
            metadata=response_plan_metadata(
                response_plan,
                {"contract": contract, **dict(metadata or {})},
            ),
        )

    async def _support_guided_handoff(
        self,
        *,
        message: InboundMessage,
        contract: dict,
        issue_type: str,
        summary: str,
        rebooted_remotely: bool = False,
        manual_checks_completed: bool = False,
    ) -> AgentResult:
        """Devuelve el handoff support guided."""
        return await self._human_handoff(
            message,
            self._support_handoff_summary(
                contract=contract,
                issue_type=issue_type,
                reason=summary,
                system_detail="Cliente completó validaciones guiadas y requiere continuidad con asesor especializado."
                if manual_checks_completed
                else None,
            ),
            final_message=format_support_guided_handoff_reply(
                issue_type,
                contract,
                rebooted_remotely=rebooted_remotely,
                manual_checks_completed=manual_checks_completed,
            ),
        )

    @staticmethod
    def _set_support_manual_followup(support_state: dict, *, issue_type: str, failure_summary: str | None = None) -> None:
        """Devuelve el followup set support manual."""
        support_state["last_system_issue"] = failure_summary
        ContactSupportMixin._clear_support_conversation_memory(support_state)
        support_state["manual_checks_requested"] = True
        support_state["manual_checks_confirmed"] = False
        if issue_type == "generic_network":
            support_state["awaiting_issue_type"] = True
            support_state["awaiting_resolution_confirmation"] = False
            support_state["last_issue_type"] = None
            return
        support_state["awaiting_issue_type"] = False
        support_state["awaiting_resolution_confirmation"] = True
        support_state["last_issue_type"] = issue_type

    async def _build_support_manual_checks_result(
        self,
        *,
        message: InboundMessage,
        contract: dict,
        support_state: dict,
        issue_type: str,
        failure_summary: str,
    ) -> AgentResult:
        """Construye resultado support manual checks a partir del contexto disponible."""
        if support_state.get("manual_checks_confirmed"):
            support_state["manual_checks_requested"] = False
            support_state["manual_checks_confirmed"] = False
            support_state["last_system_issue"] = None
            return await self._support_guided_handoff(
                message=message,
                contract=contract,
                issue_type=issue_type,
                summary=failure_summary,
                manual_checks_completed=True,
            )
        self._set_support_manual_followup(
            support_state,
            issue_type=issue_type,
            failure_summary=failure_summary,
        )
        message_text = (
            format_support_issue_triage_reply(contract)
            if issue_type == "generic_network"
            else format_support_manual_checks_reply(issue_type, contract)
        )
        intent = "support_clarify" if issue_type == "generic_network" else "support_manual_checks"
        logger.info(
            "support_manual_checks_requested contract=%s issue_type=%s failure=%r",
            contract.get("code"),
            issue_type,
            failure_summary[:220],
        )
        response_plan = build_support_response_plan(
            message=message_text,
            contract=contract,
            issue_type=issue_type,
            conversation_state="manual_checks",
            diagnostic_context={
                "hypothesis": "manual_checks_required",
                "next_step": "confirm_after_manual_checks",
                "followup_prompt": "Cuando termine, indíqueme si ya quedó bien o si todavía sigue igual.",
            },
            followup_prompt="Cuando termine, indíqueme si ya quedó bien o si todavía sigue igual.",
            reply_goal="pedir validaciones básicas sin sonar a script",
        )
        self._store_support_response_plan(support_state, response_plan)
        return AgentResult(
            message=message_text,
            intent=intent,
            agent="support",
            metadata=response_plan_metadata(
                response_plan,
                {
                "contract": contract,
                "issue_type": issue_type,
                "system_failure": failure_summary,
                },
            ),
        )

    async def _start_edit_network_flow(
        self,
        *,
        message: InboundMessage,
        state: SessionState,
        contract: dict,
        support_state: dict,
    ) -> AgentResult:
        """Inicia edit network flow."""
        out = await self.otp.generate_otp(message.recipient, message.session_id, message.cedula or state.cedula)
        logger.info(
            "support_edit_network_otp session_id=%s contract=%s ok=%s",
            message.session_id,
            contract.get("code"),
            out.get("ok"),
        )
        if out.get("ok"):
            support_state["awaiting_otp"] = True
            support_state["awaiting_credentials"] = False
            support_state["awaiting_issue_type"] = False
            support_state["awaiting_resolution_confirmation"] = False
            support_state["last_issue_type"] = "edit_network"
            support_state["pending_contract"] = contract.get("code")
            return self._support_response(
                support_state=support_state,
                contract=contract,
                message=(
                    "Listo. Le envié un código de verificación a su correo. "
                    "Cuando lo tenga, compártamelo por aquí y seguimos con el cambio de su wifi."
                ),
                intent="support_edit_network_otp_sent",
                conversation_state="edit_network_otp_sent",
                issue_type="edit_network",
                diagnostic_context={"hypothesis": "otp_sent_to_customer", "next_step": "await_otp_code"},
                followup_prompt="Cuando lo tenga, compártame el código por aquí y seguimos.",
                reply_goal="pedir el OTP sin sonar a trámite",
                metadata={"otp": out},
            )
        return await self._human_handoff(
            message,
            self._support_handoff_summary(
                contract=contract,
                issue_type="edit_network",
                reason="No se pudo enviar el OTP para continuar con la edición remota del wifi.",
            ),
        )

    @staticmethod
    def _power_requires_handoff(power_dbm: float | None) -> bool:
        """Devuelve el handoff power requires."""
        return power_dbm is not None and power_dbm <= -27 and power_dbm > -28

    @staticmethod
    def _build_support_monitoring_message(
        *,
        issue_type: str,
        contract: dict,
        connected_devices: int | None,
        rebooted: bool,
        proactive_reboot: bool,
    ) -> str:
        """Construye mensaje support monitoring a partir del contexto disponible."""
        if rebooted:
            return format_support_recovery_reply(contract, proactive=proactive_reboot)
        if issue_type in {"slow_internet", "generic_network"} and connected_devices is not None:
            return format_support_device_count_reply(issue_type, contract, connected_devices)
        return format_support_monitoring_reply(issue_type, contract)

    async def _run_network_monitoring(
        self,
        *,
        message: InboundMessage,
        contract: dict,
        support_state: dict,
        issue_type: str,
    ) -> AgentResult:
        """Ejecuta network monitoring con la configuracion actual."""
        logger.info(
            "support_monitoring_start session_id=%s contract=%s issue_type=%s smart_enabled=%s",
            message.session_id,
            contract.get("code"),
            issue_type,
            self.smart.enabled(),
        )
        if not self.smart.enabled():
            return self._support_response(
                support_state=support_state,
                contract=contract,
                message=(
                    "Ahora mismo no pude correr el monitoreo automático desde aquí. "
                    "Si lo prefiere, seguimos con una validación básica por este chat o lo derivo con un asesor especializado."
                ),
                intent="support_network_pending",
                conversation_state="monitoring_unavailable",
                issue_type=issue_type,
                diagnostic_context={"hypothesis": "automatic_monitoring_unavailable", "next_step": "offer_manual_path"},
                followup_prompt="Indíqueme qué está ocurriendo y seguimos por aquí con una validación manual.",
                reply_goal="ser transparente cuando el monitoreo no está disponible",
            )
        try:
            monitor_out = await self.smart.monitor_contract(contract.get("code"))
        except CONTACT_FLOW_EXTERNAL_ERRORS:
            logger.exception("smart_monitor_contract_failed contract=%s", contract.get("code"))
            monitor_out = {"ok": False, "error": "smart_monitor_contract_failed"}
        if not monitor_out.get("ok"):
            return await self._build_support_manual_checks_result(
                message=message,
                contract=contract,
                support_state=support_state,
                issue_type=issue_type,
                failure_summary=(
                    f"No se pudo ejecutar el monitoreo inicial del contrato {contract.get('code')} "
                    f"para el caso de {support_issue_label(issue_type)}."
                ),
            )
        smart_snapshot = self.smart.extract_monitor_snapshot(monitor_out)
        onu_out = None
        onu_status = None
        power_dbm = None
        connected_devices = None
        connected_device_summary: dict = {}
        rebooted = False
        proactive_reboot = False
        status_query_failed = False
        onu_enabled = self.onu.enabled()
        if onu_enabled:
            try:
                onu_out = await self.onu.get_status(contract.get("code"))
                onu_status = onu_out.get("status")
                power_dbm = onu_out.get("power_dbm")
            except CONTACT_FLOW_EXTERNAL_ERRORS:
                logger.exception("onu_status_failed contract=%s", contract.get("code"))
                onu_out = {"ok": False, "error": "onu_status_failed"}
                status_query_failed = True
                if issue_type not in {"intermittence", "generic_network"}:
                    return await self._build_support_manual_checks_result(
                        message=message,
                        contract=contract,
                        support_state=support_state,
                        issue_type=issue_type,
                        failure_summary=(
                            f"No se pudo consultar el estado ONU del contrato {contract.get('code')} "
                            f"durante el caso de {support_issue_label(issue_type)}."
                        ),
                    )

        recovery = None
        should_reboot = issue_type in {"intermittence", "generic_network"} or onu_status in {"los", "dyinggasp"}
        if should_reboot:
            rebooted = True
            proactive_reboot = issue_type in {"intermittence", "generic_network"} and onu_status not in {"los", "dyinggasp"}
            recovery = {}
            if onu_enabled:
                try:
                    recovery["onu_reboot"] = await self.onu.reboot(contract.get("code"))
                except CONTACT_FLOW_EXTERNAL_ERRORS:
                    logger.exception("onu_reboot_failed contract=%s", contract.get("code"))
                    return await self._build_support_manual_checks_result(
                        message=message,
                        contract=contract,
                        support_state=support_state,
                        issue_type=issue_type,
                        failure_summary=(
                            f"No se pudo completar el reinicio remoto de la ONU del contrato {contract.get('code')} "
                            f"durante el caso de {support_issue_label(issue_type)}."
                        ),
                    )
            else:
                recovery["onu_reboot"] = {"ok": False, "skipped": "onu_disabled"}
            try:
                recovery["router_reboot"] = await self.smart.reboot_router_for_contract(contract.get("code"))
            except CONTACT_FLOW_EXTERNAL_ERRORS:
                logger.exception("smart_reboot_router_failed contract=%s", contract.get("code"))
                return await self._build_support_manual_checks_result(
                    message=message,
                    contract=contract,
                    support_state=support_state,
                    issue_type=issue_type,
                    failure_summary=(
                        f"No se pudo completar el reinicio remoto del router del contrato {contract.get('code')} "
                        f"durante el caso de {support_issue_label(issue_type)}."
                    ),
                )
            if onu_enabled:
                try:
                    onu_after = await self.onu.get_status(contract.get("code"))
                except CONTACT_FLOW_EXTERNAL_ERRORS:
                    logger.exception("onu_status_post_reboot_failed contract=%s", contract.get("code"))
                    return await self._build_support_manual_checks_result(
                        message=message,
                        contract=contract,
                        support_state=support_state,
                        issue_type=issue_type,
                        failure_summary=(
                            f"No se pudo validar el estado de la ONU del contrato {contract.get('code')} "
                            "después del reinicio remoto."
                        ),
                    )
                if onu_after.get("ok"):
                    onu_status = onu_after.get("status") or onu_status
                    power_dbm = onu_after.get("power_dbm") if onu_after.get("power_dbm") is not None else power_dbm
                    recovery["onu_after"] = onu_after
                elif issue_type in {"intermittence", "generic_network"}:
                    return await self._build_support_manual_checks_result(
                        message=message,
                        contract=contract,
                        support_state=support_state,
                        issue_type=issue_type,
                        failure_summary=(
                            f"No se pudo validar el estado de la ONU del contrato {contract.get('code')} "
                            "después del reinicio remoto."
                        ),
                    )
            if onu_status in {"los", "dyinggasp"}:
                logger.info(
                    "support_diagnostic_summary session_id=%s contract=%s issue_type=%s onu_status=%s power_dbm=%s connected_devices=%s rebooted=%s next=handoff",
                    message.session_id,
                    contract.get("code"),
                    issue_type,
                    onu_status,
                    power_dbm,
                    connected_devices,
                    rebooted,
                )
                return await self._human_handoff(
                    message,
                    self._support_handoff_summary(
                        contract=contract,
                        issue_type=issue_type,
                        reason=(
                            f"El contrato {contract.get('code')} mantiene la alerta ONU en estado {onu_status} "
                            "después del reinicio automático de ONU y router."
                        ),
                        diagnostic_context=build_support_diagnostic_context(
                            issue_type=issue_type,
                            contract=contract,
                            connected_devices=connected_devices,
                            onu_status=onu_status,
                            power_dbm=power_dbm,
                            rebooted=rebooted,
                            proactive_reboot=proactive_reboot,
                            smart_snapshot=smart_snapshot,
                            connected_device_summary=connected_device_summary,
                        ),
                    ),
                )
            if status_query_failed and issue_type in {"intermittence", "generic_network"}:
                return await self._build_support_manual_checks_result(
                    message=message,
                    contract=contract,
                    support_state=support_state,
                    issue_type=issue_type,
                    failure_summary=(
                        f"No se pudo consultar el estado ONU del contrato {contract.get('code')} "
                        f"durante el caso de {support_issue_label(issue_type)}."
                    ),
                )

        if self._power_requires_handoff(power_dbm):
            logger.info(
                "support_diagnostic_summary session_id=%s contract=%s issue_type=%s onu_status=%s power_dbm=%s connected_devices=%s rebooted=%s next=handoff",
                message.session_id,
                contract.get("code"),
                issue_type,
                onu_status,
                power_dbm,
                connected_devices,
                rebooted,
            )
            return await self._human_handoff(
                message,
                self._support_handoff_summary(
                    contract=contract,
                    issue_type=issue_type,
                    reason=(
                        f"El monitoreo del contrato {contract.get('code')} detectó una potencia ONU en el umbral {power_dbm}. "
                        "Requiere revisión especializada."
                    ),
                    diagnostic_context=build_support_diagnostic_context(
                        issue_type=issue_type,
                        contract=contract,
                        connected_devices=connected_devices,
                        onu_status=onu_status,
                        power_dbm=power_dbm,
                        rebooted=rebooted,
                        proactive_reboot=proactive_reboot,
                        smart_snapshot=smart_snapshot,
                        connected_device_summary=connected_device_summary,
                    ),
                ),
            )

        if issue_type in {"slow_internet", "generic_network"}:
            try:
                connected_out = await self.smart.connected_devices_for_contract(contract.get("code"))
            except CONTACT_FLOW_EXTERNAL_ERRORS:
                logger.exception("smart_connected_devices_failed contract=%s", contract.get("code"))
                return await self._build_support_manual_checks_result(
                    message=message,
                    contract=contract,
                    support_state=support_state,
                    issue_type=issue_type,
                    failure_summary=(
                        f"No se pudo completar la consulta de dispositivos conectados del contrato {contract.get('code')} "
                        f"durante el caso de {support_issue_label(issue_type)}."
                    ),
                )
            if connected_out.get("ok"):
                connected_device_summary = self.smart.summarize_connected_devices_payload(connected_out)
                connected_devices = connected_device_summary.get("count")
            else:
                return await self._build_support_manual_checks_result(
                    message=message,
                    contract=contract,
                    support_state=support_state,
                    issue_type=issue_type,
                    failure_summary=(
                        f"La consulta de dispositivos conectados del contrato {contract.get('code')} "
                        f"no respondió correctamente durante el caso de {support_issue_label(issue_type)}."
                    ),
                )

        logger.info(
            "support_monitoring_done session_id=%s contract=%s issue_type=%s monitoring_ok=%s",
            message.session_id,
            contract.get("code"),
            issue_type,
            monitor_out.get("ok"),
        )
        support_state["last_system_issue"] = None
        support_state["manual_checks_requested"] = False
        support_state["manual_checks_confirmed"] = False
        logger.info(
            "support_diagnostic_summary session_id=%s contract=%s issue_type=%s onu_status=%s power_dbm=%s connected_devices=%s rebooted=%s next=followup",
            message.session_id,
            contract.get("code"),
            issue_type,
            onu_status,
            power_dbm,
            connected_devices,
            rebooted,
        )
        support_state["awaiting_issue_type"] = False
        support_state["awaiting_resolution_confirmation"] = True
        support_state["last_issue_type"] = issue_type
        diagnostic_context = build_support_diagnostic_context(
            issue_type=issue_type,
            contract=contract,
            connected_devices=connected_devices,
            onu_status=onu_status,
            power_dbm=power_dbm,
            rebooted=rebooted,
            proactive_reboot=proactive_reboot,
            smart_snapshot=smart_snapshot,
            connected_device_summary=connected_device_summary,
        )
        support_state["last_diagnostic"] = diagnostic_context
        support_state["guided_followup_attempts"] = 0
        response_plan = build_support_response_plan(
            message=str(
                diagnostic_context.get("message")
                or self._build_support_monitoring_message(
                    issue_type=issue_type,
                    contract=contract,
                    connected_devices=connected_devices,
                    rebooted=rebooted,
                    proactive_reboot=proactive_reboot,
                )
            ),
            contract=contract,
            issue_type=issue_type,
            conversation_state="monitoring_result",
            diagnostic_context=diagnostic_context,
            followup_prompt=str(diagnostic_context.get("followup_prompt") or ""),
        )
        response_plan_dump = self._store_support_response_plan(support_state, response_plan)
        return AgentResult(
            message=str(response_plan.message),
            intent="support_network_monitoring",
            agent="support",
            metadata=response_plan_metadata(
                response_plan,
                {
                "contract": contract,
                "issue_type": issue_type,
                "monitoring": monitor_out,
                "monitor_snapshot": smart_snapshot,
                "onu": onu_out,
                "recovery": recovery,
                "connected_devices": connected_devices,
                "connected_device_summary": connected_device_summary,
                "diagnostic_context": diagnostic_context,
                "support_conversation_message": str(response_plan.message or ""),
                "support_followup_prompt": str(response_plan.followup_prompt or ""),
                "stored_response_plan": response_plan_dump,
                },
            ),
        )

    async def _handle_support(self, *, message: InboundMessage, state: SessionState, contract: dict) -> AgentResult:
        """Maneja support y avanza el flujo."""
        contact_state = self._state(state)
        support_state = contact_state.setdefault(
            "support",
            {
                "awaiting_otp": False,
                "awaiting_credentials": False,
                "awaiting_issue_type": False,
                "awaiting_resolution_confirmation": False,
                "last_issue_type": None,
                "last_system_issue": None,
                "last_diagnostic": None,
                "last_response_plan": None,
                "last_followup_prompt": None,
                "guided_followup_attempts": 0,
                "manual_checks_requested": False,
                "manual_checks_confirmed": False,
                "pending_contract": None,
            },
        )
        turn_decision = await self._interpret_active_contact_turn(
            message=message,
            state=state,
            preferred_domain="support",
            contract=contract,
            contracts=contact_state.get("contracts") or [],
        )
        turn_slots = turn_decision.slot_updates or {}
        issue_type = classify_support_issue(message.mensaje)
        interpreted_issue = str(turn_slots.get("support_issue") or "").strip()
        if interpreted_issue in {"no_service", "intermittence", "slow_internet", "generic_network", "edit_network", "human"}:
            issue_type = interpreted_issue
        logger.info(
            "support_flow_state session_id=%s contract=%s issue_type=%s awaiting_otp=%s awaiting_credentials=%s awaiting_issue_type=%s awaiting_resolution_confirmation=%s turn_action=%s",
            message.session_id,
            contract.get("code"),
            issue_type,
            support_state.get("awaiting_otp"),
            support_state.get("awaiting_credentials"),
            support_state.get("awaiting_issue_type"),
            support_state.get("awaiting_resolution_confirmation"),
            turn_decision.action,
        )

        if support_state.get("awaiting_resolution_confirmation"):
            previous_issue_type = str(support_state.get("last_issue_type") or "generic_network")
            diagnostic_context = dict(support_state.get("last_diagnostic") or {})
            followup_observations = extract_support_followup_observations(message.mensaje)
            for key in ("device_scope", "connection_type", "near_router_result", "tested_near_router", "affected_service"):
                if key in turn_slots and turn_slots.get(key) not in (None, "", False):
                    followup_observations[key] = turn_slots.get(key)
            user_reports_persisting = bool(turn_slots.get("resolution") == "persists" or is_negative(message.mensaje))
            should_escalate_after_followup = bool(
                previous_issue_type == "no_service"
                and (
                    support_state.get("last_followup_prompt")
                    or support_state.get("manual_checks_requested")
                    or support_state.get("last_diagnostic")
                )
                and (
                    user_reports_persisting
                    or user_reports_manual_checks_done(message.mensaje)
                    or str(turn_slots.get("resolution") or "").strip().lower() == "persists"
                )
            )
            if issue_type == "edit_network":
                support_state["awaiting_resolution_confirmation"] = False
                return await self._start_edit_network_flow(message=message, state=state, contract=contract, support_state=support_state)
            if is_acknowledgement(message.mensaje) and not followup_observations and not user_reports_persisting:
                prompt = str(
                    support_state.get("last_followup_prompt")
                    or "Aquí continúo. Indíqueme si ya quedó bien o si todavía sigue igual."
                )
                return self._support_response(
                    support_state=support_state,
                    contract=contract,
                    message=prompt,
                    intent="support_network_followup",
                    conversation_state="followup_waiting_reply",
                    issue_type=previous_issue_type,
                    diagnostic_context=diagnostic_context,
                    followup_prompt=prompt,
                    reply_goal="mantener el hilo cuando el cliente solo acusa recibo",
                    metadata={
                        "issue_type": previous_issue_type,
                        "diagnostic_context": diagnostic_context,
                    "support_conversation_message": prompt,
                },
            )
            if should_escalate_after_followup:
                support_state["awaiting_resolution_confirmation"] = False
                last_system_issue = str(support_state.get("last_system_issue") or "").strip()
                summary = (
                    f"Cliente confirma que el inconveniente de {support_issue_label(previous_issue_type)} continúa "
                    f"después de la validación guiada en el contrato {contract.get('code')}."
                )
                support_state["last_issue_type"] = None
                support_state["last_system_issue"] = None
                self._clear_support_conversation_memory(support_state)
                support_state["manual_checks_requested"] = False
                support_state["manual_checks_confirmed"] = False
                return await self._human_handoff(
                    message,
                    self._support_handoff_summary(
                        contract=contract,
                        issue_type=previous_issue_type,
                        reason=summary,
                        diagnostic_context=diagnostic_context,
                        observations=followup_observations,
                        system_detail=last_system_issue,
                    ),
                )
            if previous_issue_type == "slow_internet":
                followup_plan = build_support_followup_plan(
                    contract=contract,
                    diagnostic_context=diagnostic_context,
                    observations=followup_observations,
                    attempts=int(support_state.get("guided_followup_attempts") or 0),
                    user_reports_persisting=user_reports_persisting,
                )
                if bool(followup_plan.get("should_handoff")):
                    support_state["awaiting_resolution_confirmation"] = False
                    last_system_issue = str(support_state.get("last_system_issue") or "").strip()
                    summary = str(followup_plan.get("handoff_reason") or "").strip() or (
                        f"Cliente reporta que el inconveniente de {support_issue_label(previous_issue_type)} persiste en el contrato {contract.get('code')}."
                    )
                    support_state["last_issue_type"] = None
                    support_state["last_system_issue"] = None
                    self._clear_support_conversation_memory(support_state)
                    support_state["manual_checks_requested"] = False
                    support_state["manual_checks_confirmed"] = False
                    return await self._human_handoff(
                        message,
                        self._support_handoff_summary(
                            contract=contract,
                            issue_type=previous_issue_type,
                            reason=summary,
                            diagnostic_context=diagnostic_context,
                            observations=followup_observations,
                            system_detail=last_system_issue,
                        ),
                    )
                guidance = str(followup_plan.get("message") or "").strip()
                if guidance:
                    support_state["guided_followup_attempts"] = int(support_state.get("guided_followup_attempts") or 0) + 1
                    support_state["last_diagnostic"] = {
                        **diagnostic_context,
                        "last_observations": followup_observations,
                        "hypothesis": str(followup_plan.get("hypothesis") or diagnostic_context.get("hypothesis") or ""),
                        "next_step": str(followup_plan.get("next_step") or diagnostic_context.get("next_step") or ""),
                    }
                    response_plan = build_support_response_plan(
                        message=guidance,
                        contract=contract,
                        issue_type=previous_issue_type,
                        conversation_state="followup_guidance",
                        diagnostic_context=support_state["last_diagnostic"],
                        observations=followup_observations,
                        followup_prompt=str(followup_plan.get("followup_prompt") or guidance),
                    )
                    self._store_support_response_plan(support_state, response_plan)
                    return AgentResult(
                        message=str(response_plan.message),
                        intent="support_network_followup",
                        agent="support",
                        metadata=response_plan_metadata(
                            response_plan,
                            {
                            "contract": contract,
                            "issue_type": previous_issue_type,
                            "diagnostic_context": support_state["last_diagnostic"],
                            "support_conversation_message": str(response_plan.message or ""),
                            "followup_observations": followup_observations,
                            "support_followup_prompt": str(response_plan.followup_prompt or ""),
                            },
                        ),
                    )
            if turn_slots.get("resolution") == "resolved" or is_affirmative(message.mensaje):
                support_state["awaiting_resolution_confirmation"] = False
                support_state["last_issue_type"] = None
                support_state["last_system_issue"] = None
                self._clear_support_conversation_memory(support_state)
                support_state["manual_checks_requested"] = False
                support_state["manual_checks_confirmed"] = False
                response_plan = build_support_response_plan(
                    message="Perfecto. Doy por resuelto el caso. Si necesita algo más con su servicio o su wifi, escríbame y seguimos.",
                    contract=contract,
                    issue_type=previous_issue_type,
                    conversation_state="resolved",
                    diagnostic_context={"hypothesis": "issue_resolved", "next_step": "close_case"},
                    reply_goal="cerrar el caso sin sonar administrativo",
                )
                return AgentResult(
                    message=str(response_plan.message),
                    intent="support_network_resolved",
                    agent="support",
                    metadata=response_plan_metadata(response_plan, {"contract": contract}),
                )
            if user_reports_persisting:
                support_state["awaiting_resolution_confirmation"] = False
                last_system_issue = str(support_state.get("last_system_issue") or "").strip()
                summary = (
                    f"Cliente reporta que el inconveniente de {support_issue_label(str(support_state.get('last_issue_type') or 'generic_network'))} "
                    f"persiste después del monitoreo inicial. Contrato: {contract.get('code')}."
                )
                issue_for_handoff = str(support_state.get("last_issue_type") or "generic_network")
                support_state["last_issue_type"] = None
                support_state["last_system_issue"] = None
                self._clear_support_conversation_memory(support_state)
                support_state["manual_checks_requested"] = False
                support_state["manual_checks_confirmed"] = False
                return await self._human_handoff(
                    message,
                    self._support_handoff_summary(
                        contract=contract,
                        issue_type=issue_for_handoff,
                        reason=summary,
                        diagnostic_context=diagnostic_context,
                        observations=followup_observations,
                        system_detail=last_system_issue,
                    ),
                )
            if issue_type in {"no_service", "intermittence", "slow_internet", "generic_network"}:
                support_state["awaiting_resolution_confirmation"] = False
                if issue_type == previous_issue_type:
                    last_system_issue = str(support_state.get("last_system_issue") or "").strip()
                    summary = (
                        f"Cliente mantiene el inconveniente de {support_issue_label(issue_type)} "
                        f"después del monitoreo inicial. Contrato: {contract.get('code')}."
                    )
                    support_state["last_issue_type"] = None
                    support_state["last_system_issue"] = None
                    self._clear_support_conversation_memory(support_state)
                    support_state["manual_checks_requested"] = False
                    support_state["manual_checks_confirmed"] = False
                    return await self._human_handoff(
                        message,
                        self._support_handoff_summary(
                            contract=contract,
                            issue_type=issue_type,
                            reason=summary,
                            diagnostic_context=diagnostic_context,
                            observations=followup_observations,
                            system_detail=last_system_issue,
                        ),
                    )
                return await self._run_network_monitoring(
                    message=message,
                    contract=contract,
                    support_state=support_state,
                    issue_type=issue_type,
                )
            if is_support_greeting(message.mensaje):
                prompt = str(
                    support_state.get("last_followup_prompt")
                    or "Aquí sigo. Indíqueme si ya quedó bien o si todavía sigue igual."
                )
                return self._support_response(
                    support_state=support_state,
                    contract=contract,
                    message=prompt,
                    intent="support_network_followup",
                    conversation_state="followup_waiting_reply",
                    issue_type=previous_issue_type,
                    diagnostic_context=diagnostic_context,
                    followup_prompt=prompt,
                    reply_goal="retomar la conversación sin repetir un script",
                    metadata={
                        "issue_type": previous_issue_type,
                        "diagnostic_context": diagnostic_context,
                    "support_conversation_message": prompt,
                },
            )
            support_state["awaiting_resolution_confirmation"] = False
            last_system_issue = str(support_state.get("last_system_issue") or "").strip()
            summary = (
                f"Cliente respondió después de la validación guiada y el inconveniente de {support_issue_label(previous_issue_type)} "
                f"permanece en el contrato {contract.get('code')}."
            )
            support_state["last_issue_type"] = None
            support_state["last_system_issue"] = None
            self._clear_support_conversation_memory(support_state)
            support_state["manual_checks_requested"] = False
            support_state["manual_checks_confirmed"] = False
            return await self._human_handoff(
                message,
                self._support_handoff_summary(
                    contract=contract,
                    issue_type=previous_issue_type,
                    reason=summary,
                    diagnostic_context=diagnostic_context,
                    observations=followup_observations,
                    system_detail=last_system_issue,
                ),
            )

        if support_state.get("awaiting_credentials"):
            if turn_slots.get("show_current_networks") is True or user_requests_current_network_names(message.mensaje):
                if not self.smart.enabled():
                    return self._support_response(
                        support_state=support_state,
                        contract=contract,
                        message=EDIT_NETWORK_LIST_FALLBACK_PROMPT,
                        intent="support_edit_network_credentials",
                        conversation_state="edit_network_credentials",
                        issue_type="edit_network",
                        diagnostic_context={"hypothesis": "network_lookup_unavailable", "next_step": "collect_new_network_credentials"},
                        followup_prompt="Envíame el nuevo nombre base y la nueva contraseña para continuar.",
                        reply_goal="seguir con el cambio de wifi aunque no pude leer los nombres actuales",
                    )
                try:
                    redes_out = await self.smart.list_networks_for_contract(contract.get("code"))
                except CONTACT_FLOW_EXTERNAL_ERRORS:
                    logger.exception("smart_list_networks_failed contract=%s", contract.get("code"))
                    redes_out = {"ok": False, "error": "smart_list_networks_failed"}
                if redes_out.get("ok"):
                    summary = self.smart.describe_primary_networks((redes_out.get("data") or {}))
                    if summary:
                        composed = f"{summary}\n\n{EDIT_NETWORK_AFTER_LIST_PROMPT}"
                        return self._support_response(
                            support_state=support_state,
                            contract=contract,
                            message=composed,
                            intent="support_edit_network_credentials",
                            conversation_state="edit_network_credentials",
                            issue_type="edit_network",
                            diagnostic_context={"hypothesis": "customer_requested_current_network_names", "next_step": "collect_new_network_credentials"},
                            followup_prompt="Ahora envíeme el nuevo nombre base y la nueva contraseña.",
                            reply_goal="mostrar las redes actuales y continuar el cambio con naturalidad",
                            metadata={"networks": redes_out},
                        )
                return self._support_response(
                    support_state=support_state,
                    contract=contract,
                    message=EDIT_NETWORK_LIST_FALLBACK_PROMPT,
                    intent="support_edit_network_credentials",
                    conversation_state="edit_network_credentials",
                    issue_type="edit_network",
                    diagnostic_context={"hypothesis": "network_lookup_failed", "next_step": "collect_new_network_credentials"},
                    followup_prompt="Envíeme el nuevo nombre base y la nueva contraseña para continuar.",
                    reply_goal="seguir con el cambio aunque no pude listar las redes actuales",
                )
            nombre = str(turn_slots.get("network_name") or "").strip()
            pwd = str(turn_slots.get("network_password") or "").strip()
            if not nombre or not pwd:
                nombre, pwd = parse_edit_redes_payload(message.mensaje)
            if not nombre or not pwd:
                return self._support_response(
                    support_state=support_state,
                    contract=contract,
                    message=EDIT_NETWORK_RETRY_PROMPT,
                    intent="support_edit_network_credentials",
                    conversation_state="edit_network_credentials",
                    issue_type="edit_network",
                    diagnostic_context={"hypothesis": "missing_network_credentials", "next_step": "collect_new_network_credentials"},
                    followup_prompt="Envíemelo así: nombre y contraseña.",
                    reply_goal="pedir los datos del nuevo wifi de forma clara y simple",
                )
            if self.smart.enabled():
                try:
                    out = await self.smart.change_networks_for_contract(contract.get("code"), nombre, pwd)
                except CONTACT_FLOW_EXTERNAL_ERRORS:
                    logger.exception("smart_change_networks_failed contract=%s", contract.get("code"))
                    out = {"ok": False, "error": "smart_change_networks_failed"}
                if out.get("ok"):
                    support_state["awaiting_credentials"] = False
                    support_state["pending_contract"] = None
                    message_text = ((out.get("data") or {}).get("mensaje") or "Se ha realizado la modificacion de datos.").strip()
                    if message_text.lower() == "se ha realizado la modificacion de datos.":
                        message_text = "Listo, ya quedó realizado el cambio del nombre y la clave de su wifi."
                    logger.info("support_edit_network_done session_id=%s contract=%s", message.session_id, contract.get("code"))
                    return self._support_response(
                        support_state=support_state,
                        contract=contract,
                        message=message_text,
                        intent="support_edit_network_done",
                        conversation_state="edit_network_done",
                        issue_type="edit_network",
                        diagnostic_context={"hypothesis": "network_credentials_updated", "next_step": "offer_more_help"},
                        reply_goal="confirmar el cambio del wifi con tono humano",
                        metadata={"change_result": out},
                    )
                return await self._human_handoff(
                    message,
                    self._support_handoff_summary(
                        contract=contract,
                        issue_type="edit_network",
                        reason=f"Ocurrió un problema al procesar el cambio de redes para el contrato {contract.get('code')}.",
                    ),
                )
            return self._support_response(
                support_state=support_state,
                contract=contract,
                message=(
                    "Puedo seguir con su caso, pero en este ambiente todavía no tengo habilitado el cambio remoto de redes. "
                    "Si lo prefiere, lo derivo con un asesor especializado para completarlo."
                ),
                intent="support_edit_network_pending",
                conversation_state="edit_network_pending",
                issue_type="edit_network",
                diagnostic_context={"hypothesis": "remote_network_change_unavailable", "next_step": "offer_handoff"},
                reply_goal="ser transparente sobre la limitación del ambiente",
            )

        otp_len = self.otp.settings.otp_code_len
        normalized = str(turn_slots.get("otp_code") or "").strip() or (message.mensaje or "").strip()
        if support_state.get("awaiting_otp"):
            if turn_slots.get("otp_missing") is True or user_reports_missing_otp(normalized):
                support_state["awaiting_otp"] = False
                return await self._human_handoff(
                    message,
                    self._support_handoff_summary(
                        contract=contract,
                        issue_type="edit_network",
                        reason=f"Cliente reporta no disponer del OTP para el contrato {contract.get('code')}.",
                    ),
                )
            if normalized and len(normalized) == otp_len and all(char in "0123456789ABCDEFabcdef" for char in normalized):
                out = await self.otp.verify_otp(message.recipient, message.session_id, normalized)
                if out.get("ok") and out.get("data", {}).get("verified") is True:
                    logger.info("support_otp_verified session_id=%s contract=%s", message.session_id, contract.get("code"))
                    if self.smart.enabled():
                        support_state["awaiting_otp"] = False
                        support_state["awaiting_credentials"] = True
                        support_state["pending_contract"] = contract.get("code")
                        logger.info("support_edit_network_ready session_id=%s contract=%s", message.session_id, contract.get("code"))
                        return self._support_response(
                            support_state=support_state,
                            contract=contract,
                            message=EDIT_NETWORK_READY_PROMPT,
                            intent="support_edit_network_ready",
                            conversation_state="edit_network_ready",
                            issue_type="edit_network",
                            diagnostic_context={"hypothesis": "otp_verified", "next_step": "collect_new_network_credentials"},
                            followup_prompt="Envíeme el nuevo nombre base y la nueva contraseña.",
                            reply_goal="pasar del OTP al cambio de wifi con continuidad",
                        )
                    return self._support_response(
                        support_state=support_state,
                        contract=contract,
                        message=(
                            "El código quedó validado, pero en este ambiente todavía no tengo habilitado el cambio remoto de redes. "
                            "Si lo prefiere, lo derivo con un asesor especializado para completarlo."
                        ),
                        intent="support_edit_network_pending",
                        conversation_state="edit_network_pending",
                        issue_type="edit_network",
                        diagnostic_context={"hypothesis": "otp_verified_but_network_change_unavailable", "next_step": "offer_handoff"},
                        reply_goal="explicar con claridad por qué no puedo seguir por este canal",
                    )

                locked = out.get("data", {}).get("locked")
                attempts_left = out.get("data", {}).get("attempts_left")
                logger.info(
                    "support_otp_retry session_id=%s contract=%s locked=%s attempts_left=%s",
                    message.session_id,
                    contract.get("code"),
                    locked,
                    attempts_left,
                )
                if locked:
                    support_state["awaiting_otp"] = False
                    return await self._human_handoff(
                        message,
                        self._support_handoff_summary(
                            contract=contract,
                            issue_type="edit_network",
                            reason=f"OTP bloqueado para contrato {contract.get('code')}.",
                        ),
                    )
                return self._support_response(
                    support_state=support_state,
                    contract=contract,
                    message=f"El código no coincide. Le quedan {attempts_left} intentos. Revíselo y vuelva a enviármelo.",
                    intent="support_edit_network_otp_retry",
                    conversation_state="edit_network_otp_retry",
                    issue_type="edit_network",
                    diagnostic_context={"hypothesis": "otp_incorrect", "next_step": "await_otp_retry"},
                    followup_prompt="Revíselo en su correo y vuelva a enviármelo por aquí.",
                    reply_goal="pedir el OTP otra vez sin sonar seco",
                    metadata={"otp": out},
                )

            return self._support_response(
                support_state=support_state,
                contract=contract,
                message="Aquí continúo. Revise su correo y envíeme el código OTP para continuar con el cambio de redes.",
                intent="support_edit_network_waiting_otp",
                conversation_state="edit_network_waiting_otp",
                issue_type="edit_network",
                diagnostic_context={"hypothesis": "awaiting_otp_code", "next_step": "wait_otp"},
                followup_prompt="Cuando tenga el código, envíemelo por aquí y seguimos.",
                reply_goal="mantener el hilo mientras espero el OTP",
            )
        if support_state.get("awaiting_issue_type"):
            if issue_type == "human":
                support_state["awaiting_issue_type"] = False
                support_state["last_system_issue"] = None
                self._clear_support_conversation_memory(support_state)
                support_state["manual_checks_requested"] = False
                support_state["manual_checks_confirmed"] = False
                return await self._human_handoff(
                    message,
                    self._support_handoff_summary(
                        contract=contract,
                        issue_type="generic_network",
                        reason=f"Cliente solicita asesor especializado. Contrato seleccionado: {contract.get('code')}.",
                    ),
                )
            if issue_type == "edit_network":
                support_state["awaiting_issue_type"] = False
                support_state["last_system_issue"] = None
                self._clear_support_conversation_memory(support_state)
                support_state["manual_checks_requested"] = False
                support_state["manual_checks_confirmed"] = False
                return await self._start_edit_network_flow(message=message, state=state, contract=contract, support_state=support_state)
            if issue_type in {"no_service", "intermittence", "slow_internet"}:
                support_state["awaiting_issue_type"] = False
                support_state["manual_checks_confirmed"] = user_reports_manual_checks_done(message.mensaje)
                return await self._run_network_monitoring(
                    message=message,
                    contract=contract,
                    support_state=support_state,
                    issue_type=issue_type,
                )
            if is_support_greeting(message.mensaje):
                response_plan = build_support_response_plan(
                    message=format_support_issue_nudge(contract),
                    contract=contract,
                    issue_type="generic_network",
                    conversation_state="clarify_issue",
                    diagnostic_context={"hypothesis": "needs_issue_detail", "next_step": "classify_issue_type"},
                    reply_goal="pedir una precisión corta sin sonar a menú",
                )
                return AgentResult(
                    message=str(response_plan.message),
                    intent="support_clarify",
                    agent="support",
                    metadata=response_plan_metadata(response_plan, {"contract": contract}),
                )
            response_plan = build_support_response_plan(
                message=format_support_issue_triage_reply(contract),
                contract=contract,
                issue_type="generic_network",
                conversation_state="clarify_issue",
                diagnostic_context={"hypothesis": "needs_issue_detail", "next_step": "classify_issue_type"},
                reply_goal="pedir una precisión técnica útil",
            )
            return AgentResult(
                message=str(response_plan.message),
                intent="support_clarify",
                agent="support",
                metadata=response_plan_metadata(response_plan, {"contract": contract}),
            )
        if issue_type == "human":
            return await self._human_handoff(
                message,
                self._support_handoff_summary(
                    contract=contract,
                    issue_type="generic_network",
                    reason=f"Cliente solicita asesor especializado. Contrato seleccionado: {contract.get('code')}.",
                ),
            )
        if issue_type == "edit_network":
            return await self._start_edit_network_flow(message=message, state=state, contract=contract, support_state=support_state)
        if issue_type == "generic_network":
            support_state["awaiting_issue_type"] = True
            support_state["last_system_issue"] = None
            self._clear_support_conversation_memory(support_state)
            support_state["manual_checks_requested"] = True
            support_state["manual_checks_confirmed"] = False
            response_plan = build_support_response_plan(
                message=format_support_issue_triage_reply(contract),
                contract=contract,
                issue_type="generic_network",
                conversation_state="clarify_issue",
                diagnostic_context={"hypothesis": "needs_issue_detail", "next_step": "classify_issue_type"},
                reply_goal="entender el problema antes de seguir con monitoreo",
            )
            return AgentResult(
                message=str(response_plan.message),
                intent="support_clarify",
                agent="support",
                metadata=response_plan_metadata(response_plan, {"contract": contract}),
            )
        if issue_type in {"no_service", "intermittence", "slow_internet"}:
            if (
                issue_type == "no_service"
                and str(support_state.get("last_issue_type") or "").strip() == "no_service"
                and (
                    support_state.get("last_diagnostic")
                    or support_state.get("last_followup_prompt")
                    or support_state.get("awaiting_resolution_confirmation")
                )
            ):
                support_state["awaiting_resolution_confirmation"] = False
                last_system_issue = str(support_state.get("last_system_issue") or "").strip()
                summary = (
                    f"Cliente reporta nuevamente que sigue sin internet en el contrato {contract.get('code')} "
                    "después del monitoreo inicial."
                )
                support_state["last_issue_type"] = None
                support_state["last_system_issue"] = None
                self._clear_support_conversation_memory(support_state)
                support_state["manual_checks_requested"] = False
                support_state["manual_checks_confirmed"] = False
                return await self._human_handoff(
                    message,
                    self._support_handoff_summary(
                        contract=contract,
                        issue_type=issue_type,
                        reason=summary,
                        diagnostic_context=build_support_diagnostic_context(
                            issue_type=issue_type,
                            contract=contract,
                            connected_devices=None,
                            onu_status=None,
                            power_dbm=None,
                            rebooted=False,
                            proactive_reboot=False,
                            smart_snapshot=None,
                            connected_device_summary=None,
                        ),
                        system_detail=last_system_issue,
                    ),
                )
            return await self._run_network_monitoring(
                message=message,
                contract=contract,
                support_state=support_state,
                issue_type=issue_type,
            )

        support_state["awaiting_issue_type"] = True
        response_plan = build_support_response_plan(
            message=format_support_clarification(contract),
            contract=contract,
            issue_type="generic_network",
            conversation_state="clarify_issue",
            diagnostic_context={"hypothesis": "needs_issue_detail", "next_step": "classify_issue_type"},
            reply_goal="entender rápido el tipo de problema para guiar mejor",
        )
        return AgentResult(
            message=str(response_plan.message),
            intent="support_clarify",
            agent="support",
            metadata=response_plan_metadata(response_plan, {"contract": contract}),
        )
