"""ayudas del flujo comercial."""

import logging
import re
from typing import Any

from packages.agents.sales.constants import (
    AGENCY_PROMPT,
    COMMERCIAL_FOLLOWUP_MSG,
    COMMERCIAL_INFO_CHOICE_MSG,
    COMMERCIAL_INFO_ONLY_MSG,
    COMMERCIAL_RECOMMENDATION_INFO_ONLY_MSG,
    COMMERCIAL_RECOMMENDATION_MSG,
    CRM_CREATED_MSG,
    CRM_PENDING_MSG,
    CRM_QUESTIONS,
    LOCATION_FALLBACK_PROMPT,
    MAIN_MENU_ACTIONS,
    MENU,
    PAYMENTS_REDIRECT_MSG,
    SALES_EXTERNAL_ERRORS,
)
from packages.agents.sales.recommendation_utils import (
    build_recommendation_retry_message,
    build_recommendation_step_message,
    build_recommendation_message,
    build_recommended_plan_capture_prefix,
    build_recommended_plan_followup_prompt,
    extract_recommendation_slot_updates,
    merge_recommendation_profile,
    next_recommendation_field,
    recommend_plan,
    recommendation_question,
    sanitize_recommendation_slot_updates,
)
from packages.agents.sales.utils import (
    _extract_location_from_text,
    _looks_like_precise_address,
    _street_candidate,
    _strip_agency_words,
    _title_case,
    norm,
)
from packages.shared.response_planner import build_sales_response_plan
from packages.shared.utils import contains_any_phrase, normalize_text
from packages.shared.schemas import AgentResult, FlowTurnInterpretation

logger = logging.getLogger("agents.sales")

CATALOG_PRICE_PATTERN = re.compile(r"(?:\$|usd\s*)?(\d{1,4}(?:[.,]\d{1,2})?)", re.IGNORECASE)
CATALOG_SPACED_PRICE_PATTERN = re.compile(r"\b(\d{1,4})\s+(\d{2})\b")
CATALOG_BANDWIDTH_PATTERN = re.compile(r"\b(\d{2,4})\s*(?:megas?|mbps?)\b")
CATALOG_ORDINAL_ALIASES = {
    "primer": 1,
    "primero": 1,
    "primera": 1,
    "segundo": 2,
    "segunda": 2,
    "tercero": 3,
    "tercera": 3,
    "cuarto": 4,
    "cuarta": 4,
}
HIGHER_SPEED_COMPARISON_TERMS = (
    "mas megas",
    "más megas",
    "con mas megas",
    "con más megas",
    "mas velocidad",
    "más velocidad",
    "mayor velocidad",
    "mas rapido",
    "más rapido",
    "mas rápido",
    "más rápido",
    "algo mas rapido",
    "algo más rapido",
    "algo mas rápido",
    "algo más rápido",
    "mas rapido que ese",
    "más rapido que ese",
    "más rápido que ese",
    "otro mas rapido",
    "otro más rapido",
    "otro más rápido",
    "planes con mas megas",
    "planes con más megas",
)

RECOMMENDATION_SLOT_EXTRACTION_INSTRUCTIONS = (
    "Extrae datos estructurados para recomendar un plan de internet por WhatsApp. "
    "Devuelve solo JSON valido con estas llaves: segment, people, devices, space_size, usage. "
    "Valores permitidos: "
    "segment = residential | pymes | null; "
    "people = entero o null; "
    "devices = entero o null; "
    "space_size = small | medium | large | null; "
    "usage = basic | streaming | remote_work | gaming | business_ops | cameras | null. "
    "Usa current_field, known_profile y recent_turns como contexto, pero prioriza el ultimo mensaje del cliente. "
    "Si el cliente responde con un rango como '3-4 aprox', devuelve el valor alto. "
    "Si dice 'vivo solo', people = 1. "
    "Si dice que varias personas usan varios dispositivos, puedes devolver ambos campos. "
    "Si no esta claro, devuelve null en ese campo. "
    "No inventes valores fuera del esquema."
)

RECOMMENDED_PLAN_DECISION_INSTRUCTIONS = (
    "Interpretas la respuesta del cliente despues de que un asesor recomendo un plan de internet "
    "y le pregunto si quiere avanzar con ese plan o ver otras opciones. "
    "Devuelve solo JSON valido con esta llave: decision. "
    "decision = accept | catalog | unclear. "
    "accept si el cliente quiere seguir con el plan recomendado o dejarlo avanzado. "
    "catalog si quiere comparar, ver mas opciones o ver todos los planes. "
    "unclear si no queda claro, cambia de tema o no hay suficiente senal. "
    "Usa recommended_plan y recent_turns como contexto. "
    "Se conservador y no inventes."
)

ACTIVE_COMMERCIAL_SWITCH_INTENTS = ("agencies", "menu", "payments", "payment_methods", "pago_mensual", "contacto")
ACTIVE_COMMERCIAL_ALLOWED_ACTIONS = {
    "info_choice": ("start_recommendation", "show_catalog", "start_capture", "switch_intent", "unclear"),
    "recommendation_question": ("answer_current_step", "show_catalog", "start_capture", "switch_intent", "unclear"),
    "recommended_plan_followup": ("accept_recommended_plan", "show_catalog", "switch_intent", "unclear"),
    "crm_capture": ("answer_current_step", "decline_registration", "show_catalog", "switch_intent", "unclear"),
}


class SalesCommercialHelpersMixin:
    """Agrupa la lógica comercial que decide entre catálogo, recomendación guiada y captura de datos."""

    CITY_REPLY_STREETLIKE = "street_like"
    CITY_REPLY_UNSUPPORTED = "unsupported"
    CITY_REPLY_INVALID = "invalid"
    CITY_REPLY_SAVED = "saved"

    @staticmethod
    def _clear_commercial_info_choice(sales_state: dict) -> None:
        """Limpia choice commercial info."""
        sales_state["awaiting_info_choice"] = False

    @staticmethod
    def _wants_higher_speed_options(normalized: str) -> bool:
        """Devuelve el options wants higher speed."""
        return bool(normalized) and contains_any_phrase(normalized, HIGHER_SPEED_COMPARISON_TERMS)

    def _recommendation_extractor_mode(self) -> str:
        """Lee el modo configurado para extraer datos estructurados de la recomendación."""
        return str(getattr(self.settings, "sales_recommendation_extractor_mode", "auto") or "auto").strip().lower()

    @staticmethod
    def _active_commercial_stage(sales_state: dict) -> str | None:
        """Devuelve el paso active commercial."""
        if sales_state.get("awaiting_info_choice"):
            return "info_choice"
        if sales_state.get("awaiting_recommendation_field"):
            return "recommendation_question"
        if sales_state.get("awaiting_crm_field"):
            return "crm_capture"
        if sales_state.get("recommended_plan"):
            return "recommended_plan_followup"
        return None

    def _active_commercial_question(self, sales_state: dict, stage: str | None) -> str | None:
        """Devuelve la pregunta active commercial."""
        if stage == "info_choice":
            return "¿Desea ver todos los planes o prefiere una recomendación personalizada?"
        if stage == "recommendation_question":
            current_field = sales_state.get("awaiting_recommendation_field")
            return recommendation_question(current_field, sales_state.get("recommendation_profile") or {})
        if stage == "crm_capture":
            return CRM_QUESTIONS.get(sales_state.get("awaiting_crm_field"))
        if stage == "recommended_plan_followup":
            return build_recommended_plan_followup_prompt(
                sales_state.get("recommended_plan") or {},
                sales_state.get("recommendation_profile") or {},
            )
        return None

    def _active_commercial_known_state(self, sales_state: dict, stage: str | None) -> dict:
        """Devuelve el estado active commercial known."""
        known_state = {"lead": dict(sales_state.get("lead") or {})}
        if stage == "info_choice":
            known_state["commercial_registration_declined"] = bool(sales_state.get("commercial_registration_declined"))
        if stage == "recommendation_question":
            known_state["current_field"] = sales_state.get("awaiting_recommendation_field")
            known_state["recommendation_profile"] = dict(sales_state.get("recommendation_profile") or {})
        if stage == "recommended_plan_followup":
            known_state["recommended_plan"] = dict(sales_state.get("recommended_plan") or {})
            known_state["recommendation_profile"] = dict(sales_state.get("recommendation_profile") or {})
        if stage == "crm_capture":
            known_state["current_field"] = sales_state.get("awaiting_crm_field")
            known_state["commercial_catalog_segment"] = sales_state.get("commercial_catalog_segment")
        return known_state

    def _active_commercial_recent_turns(self, sales_state: dict) -> list[dict[str, str]]:
        """Devuelve los turnos recientes del flujo comercial activo."""
        recent_turns: list[dict[str, str]] = []
        for item in (sales_state.get("history") or [])[-6:]:
            role = str((item or {}).get("role") or "").strip().lower()
            content = str((item or {}).get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                recent_turns.append({"role": role, "content": content[:220]})
        return recent_turns

    def _fallback_active_commercial_turn(
        self,
        *,
        sales_state: dict,
        user_message: str,
        analysis,
        stage: str,
    ) -> FlowTurnInterpretation:
        """Devuelve el turno fallback active commercial."""
        intent = analysis.intent
        current_recommendation_field = sales_state.get("awaiting_recommendation_field")
        if intent in ACTIVE_COMMERCIAL_SWITCH_INTENTS:
            if (
                stage == "recommendation_question"
                and current_recommendation_field == "devices"
                and extract_recommendation_slot_updates(user_message, current_field=current_recommendation_field).get("devices")
                is not None
            ):
                return FlowTurnInterpretation(action="answer_current_step", reason="current_step_device_answer")
            return FlowTurnInterpretation(action="switch_intent", target_intent=intent, reason="explicit_global_intent")

        if stage == "info_choice":
            profile_updates = extract_recommendation_slot_updates(user_message)
            if analysis.catalog_segment and not profile_updates.get("segment"):
                profile_updates["segment"] = analysis.catalog_segment
            if analysis.wants_capture:
                return FlowTurnInterpretation(action="start_capture", reason="explicit_capture_request")
            if self._choice_reply_wants_full_catalog(analysis, profile_updates):
                return FlowTurnInterpretation(action="show_catalog", reason="catalog_choice")
            if self._choice_reply_wants_recommendation(analysis, profile_updates):
                return FlowTurnInterpretation(action="start_recommendation", reason="recommendation_choice")
            return FlowTurnInterpretation(action="unclear", reason="info_choice_fallback")

        if stage == "recommendation_question":
            if analysis.wants_capture:
                return FlowTurnInterpretation(action="start_capture", reason="explicit_capture_request")
            if analysis.wants_full_catalog:
                return FlowTurnInterpretation(action="show_catalog", reason="catalog_request")
            return FlowTurnInterpretation(action="answer_current_step", reason="guided_recommendation_step")

        if stage == "recommended_plan_followup":
            fallback_decision = self._fallback_recommended_plan_decision(analysis.normalized)
            if analysis.accepts_recommended_plan or analysis.wants_capture or fallback_decision == "accept":
                return FlowTurnInterpretation(action="accept_recommended_plan", reason="recommended_plan_acceptance")
            if analysis.wants_full_catalog or fallback_decision == "catalog":
                return FlowTurnInterpretation(action="show_catalog", reason="recommended_plan_compare")
            return FlowTurnInterpretation(action="unclear", reason="recommended_plan_followup_fallback")

        if stage == "crm_capture":
            if analysis.declines_registration:
                return FlowTurnInterpretation(action="decline_registration", reason="declines_registration")
            if analysis.is_commercial_followup:
                return FlowTurnInterpretation(action="show_catalog", reason="commercial_followup_request")
            return FlowTurnInterpretation(action="answer_current_step", reason="crm_capture_step")

        return FlowTurnInterpretation(action="unclear", reason="no_stage")

    async def _interpret_active_commercial_turn(
        self,
        *,
        sales_state: dict,
        user_message: str,
        analysis,
    ) -> FlowTurnInterpretation:
        """Interpreta active commercial turn."""
        stage = self._active_commercial_stage(sales_state)
        if not stage:
            return FlowTurnInterpretation(action="unclear", reason="no_active_stage")

        fallback = self._fallback_active_commercial_turn(
            sales_state=sales_state,
            user_message=user_message,
            analysis=analysis,
            stage=stage,
        )
        if fallback.action in {
            "switch_intent",
            "show_catalog",
            "accept_recommended_plan",
            "decline_registration",
            "start_capture",
            "start_recommendation",
        }:
            return fallback
        interpreter = getattr(self, "turn_interpreter", None)
        if interpreter is None:
            return fallback

        interpretation = await interpreter.interpret(
            flow_name="sales_commercial",
            current_stage=stage,
            user_message=user_message,
            current_question=self._active_commercial_question(sales_state, stage),
            allowed_actions=ACTIVE_COMMERCIAL_ALLOWED_ACTIONS.get(stage, ("unclear",)),
            allowed_switch_intents=ACTIVE_COMMERCIAL_SWITCH_INTENTS,
            known_state=self._active_commercial_known_state(sales_state, stage),
            recent_turns=self._active_commercial_recent_turns(sales_state),
            initial_analysis={
                "intent": analysis.intent,
                "normalized": analysis.normalized,
                "wants_capture": bool(analysis.wants_capture),
                "wants_full_catalog": bool(analysis.wants_full_catalog),
                "declines_registration": bool(getattr(analysis, "declines_registration", False)),
                "accepts_recommended_plan": bool(getattr(analysis, "accepts_recommended_plan", False)),
                "is_commercial_followup": bool(getattr(analysis, "is_commercial_followup", False)),
            },
            fallback=fallback,
        )
        logger.info(
            "sales_active_turn_decision stage=%s action=%s target_intent=%s reason=%s",
            stage,
            interpretation.action,
            interpretation.target_intent,
            interpretation.reason,
        )
        return interpretation

    def _should_use_recommendation_extractor(self) -> bool:
        """Decide si conviene usar el extractor estructurado para la recomendación."""
        if not bool(getattr(self.settings, "sales_recommendation_extractor_enabled", True)):
            return False
        mode = self._recommendation_extractor_mode()
        if mode == "heuristic":
            return False
        if mode == "openai":
            return True
        if mode == "auto":
            enabled_fn = getattr(self.llm, "enabled", None)
            if callable(enabled_fn):
                try:
                    return bool(enabled_fn())
                except Exception:
                    return False
            return True
        return False

    async def _extract_recommendation_slot_updates(
        self,
        *,
        sales_state: dict,
        user_message: str,
        current_field: str | None,
    ) -> dict:
        """Extrae recommendation slot updates."""
        if not self._should_use_recommendation_extractor():
            return {}

        extract_fn = getattr(self.llm, "extract_json", None)
        if not callable(extract_fn):
            return {}

        recent_turns: list[dict[str, str]] = []
        for item in (sales_state.get("history") or [])[-6:]:
            role = str((item or {}).get("role") or "").strip().lower()
            content = str((item or {}).get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                recent_turns.append({"role": role, "content": content[:220]})

        current_profile = dict(sales_state.get("recommendation_profile") or {})
        payload = {
            "message": {
                "text": str(user_message or "").strip(),
                "normalized": normalize_text(user_message),
            },
            "recommendation_context": {
                "current_field": current_field,
                "current_question": recommendation_question(current_field, current_profile) if current_field else None,
                "known_profile": current_profile,
                "recent_turns": recent_turns,
            },
        }
        try:
            result = await extract_fn(
                instructions=RECOMMENDATION_SLOT_EXTRACTION_INSTRUCTIONS,
                payload=payload,
                max_output_tokens=140,
                temperature=0.0,
            )
        except Exception:
            logger.exception("sales_recommendation_extractor_failed current_field=%s", current_field)
            return {}

        if result.get("status") != "ok" or not isinstance(result.get("result"), dict):
            return {}

        updates = sanitize_recommendation_slot_updates(result.get("result"))
        if updates:
            logger.info("sales_recommendation_slots current_field=%s updates=%s", current_field, updates)
        return updates

    async def _merge_recommendation_profile(
        self,
        *,
        sales_state: dict,
        user_message: str,
        current_field: str | None,
    ) -> dict:
        """Fusiona el perfil actual con los nuevos datos detectados en el mensaje."""
        slot_updates = await self._extract_recommendation_slot_updates(
            sales_state=sales_state,
            user_message=user_message,
            current_field=current_field,
        )
        return merge_recommendation_profile(
            sales_state.get("recommendation_profile") or {},
            user_message,
            current_field=current_field,
            slot_updates=slot_updates,
        )

    @staticmethod
    def _fallback_recommended_plan_decision(normalized: str) -> str:
        """Resume si la respuesta apunta a aceptar el plan, volver al catálogo o seguir dudando."""
        if not normalized:
            return "unclear"
        if SalesCommercialHelpersMixin._wants_higher_speed_options(normalized):
            return "catalog"
        if contains_any_phrase(
            normalized,
            (
                "los demas",
                "los demás",
                "otros",
                "otras opciones",
                "otra opcion",
                "otra opción",
                "comparar",
                "ver mas",
                "ver más",
                "mas opciones",
                "más opciones",
            ),
        ):
            return "catalog"
        if contains_any_phrase(
            normalized,
            (
                "va ese",
                "va este",
                "me late ese",
                "me late este",
                "jalo con ese",
                "jalo con este",
                "quedemonos con ese",
                "quedémonos con ese",
                "quedemonos con este",
                "quedémonos con este",
            ),
        ):
            return "accept"
        if len(normalized.split()) <= 5 and contains_any_phrase(normalized, ("ese", "este", "ese plan", "este plan")):
            if contains_any_phrase(normalized, ("sirve", "vale", "va", "bien", "ok", "okay", "late", "funciona")):
                return "accept"
        return "unclear"

    async def _interpret_recommended_plan_followup(
        self,
        *,
        sales_state: dict,
        user_message: str,
        analysis,
    ) -> str:
        """Interpreta el seguimiento del plan recomendado."""
        if analysis.accepts_recommended_plan or analysis.wants_capture:
            return "accept"
        if analysis.wants_full_catalog:
            return "catalog"

        normalized = analysis.normalized
        fallback = self._fallback_recommended_plan_decision(normalized)
        if fallback != "unclear":
            return fallback

        if not self._should_use_recommendation_extractor():
            return "unclear"

        extract_fn = getattr(self.llm, "extract_json", None)
        if not callable(extract_fn):
            return "unclear"

        recent_turns: list[dict[str, str]] = []
        for item in (sales_state.get("history") or [])[-6:]:
            role = str((item or {}).get("role") or "").strip().lower()
            content = str((item or {}).get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                recent_turns.append({"role": role, "content": content[:220]})

        payload = {
            "message": {
                "text": str(user_message or "").strip(),
                "normalized": normalized,
            },
            "recommended_plan_context": {
                "recommended_plan": dict(sales_state.get("recommended_plan") or {}),
                "recent_turns": recent_turns,
            },
        }
        try:
            result = await extract_fn(
                instructions=RECOMMENDED_PLAN_DECISION_INSTRUCTIONS,
                payload=payload,
                max_output_tokens=80,
                temperature=0.0,
            )
        except Exception:
            logger.exception("sales_recommended_plan_decision_failed")
            return "unclear"

        if result.get("status") != "ok" or not isinstance(result.get("result"), dict):
            return "unclear"

        decision = normalize_text((result.get("result") or {}).get("decision"))
        if decision in {"accept", "catalog", "unclear"}:
            if decision != "unclear":
                logger.info("sales_recommended_plan_decision decision=%s normalized=%s", decision, normalized)
            return decision
        return "unclear"

    @staticmethod
    def _reset_commercial_recommendation(sales_state: dict) -> None:
        """Reinicia commercial recommendation para comenzar de nuevo."""
        sales_state["awaiting_recommendation_field"] = None
        sales_state["recommendation_profile"] = {}
        sales_state["recommended_plan"] = None

    def _prompt_commercial_info_choice(
        self,
        *,
        sales_state: dict,
        user_message: str,
        prefix_message: str | None = None,
    ) -> AgentResult:
        """Pide al cliente que elija entre ver catálogo o seguir una recomendación guiada."""
        sales_state["pending_intent"] = "commercial"
        sales_state["awaiting_crm_field"] = None
        sales_state["commercial_catalog_requested"] = False
        sales_state["commercial_registration_declined"] = True
        sales_state["awaiting_info_choice"] = True
        message = prefix_message or COMMERCIAL_INFO_CHOICE_MSG
        response_plan = self._sales_response_plan(
            sales_state=sales_state,
            message=message,
            conversation_state="info_choice",
            reply_goal="ayudar a elegir entre catálogo completo o recomendación guiada",
            next_step="esperar_eleccion_comercial",
            followup_prompt="Indíqueme si prefiere ver todos los planes o si desea que le recomiende uno.",
            hypothesis="needs_preference_between_catalog_and_guided_recommendation",
        )
        return self._respond(
            sales_state=sales_state,
            user_message=user_message,
            message=message,
            intent="commercial",
            metadata={
                "lead": sales_state.get("lead") or {},
                "crm_lead_created": False,
                "commercial_registration_declined": True,
            },
            response_plan=response_plan,
        )

    @staticmethod
    def _recommended_plan_capture_prefix(sales_state: dict) -> str:
        """Devuelve el prefijo para capturar el plan recomendado."""
        return build_recommended_plan_capture_prefix(
            sales_state.get("recommended_plan") or {},
            sales_state.get("recommendation_profile") or {},
        )

    def _sales_response_plan(
        self,
        *,
        sales_state: dict,
        message: str,
        conversation_state: str,
        reply_goal: str,
        next_step: str | None = None,
        followup_prompt: str | None = None,
        hypothesis: str | None = None,
    ):
        """Devuelve el plan de respuesta comercial."""
        return build_sales_response_plan(
            message=message,
            conversation_state=conversation_state,
            reply_goal=reply_goal,
            profile=sales_state.get("recommendation_profile") or {},
            recommended_plan=sales_state.get("recommended_plan") or {},
            next_step=next_step,
            followup_prompt=followup_prompt,
            hypothesis=hypothesis,
        )

    @staticmethod
    def _catalog_price_value(value: Any) -> float | None:
        """Devuelve el valor catalog precio."""
        raw = str(value or "").strip().replace(",", ".")
        if not raw:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _catalog_price_label(value: Any) -> str:
        """Devuelve la etiqueta catalog precio."""
        parsed = SalesCommercialHelpersMixin._catalog_price_value(value)
        if parsed is None:
            return "-"
        return f"{parsed:.2f}"

    @staticmethod
    def _catalog_bandwidth_value(value: Any) -> int | None:
        """Devuelve el valor catalog ancho de banda."""
        match = re.search(r"\d+", str(value or ""))
        if not match:
            return None
        return int(match.group(0))

    @staticmethod
    def _catalog_detail_names(details: list[dict[str, Any]] | None, *, limit: int = 4) -> list[str]:
        """Devuelve los names catalog detalle."""
        names = [str((item or {}).get("name") or "").strip() for item in (details or [])]
        return [name for name in names if name][:limit]

    def _display_catalog_items(self, data: dict, *, segment: str | None = None) -> list[dict[str, Any]]:
        """Devuelve el items display catalog."""
        catalog = (data or {}).get("data") or {}
        sections: list[tuple[str, list[dict[str, Any]], list[int] | None]] = []
        if segment in {None, "residential"}:
            residential = catalog.get("GONECTADOS") or []
            order = [2, 3, 1, 0]
            sections.append(("residential", residential, order))
        if segment in {None, "pymes"}:
            pymes = catalog.get("PYMES") or []
            sections.append(("pymes", pymes, None))

        plans: list[dict[str, Any]] = []
        for current_segment, items, order in sections:
            indexes = order or list(range(len(items)))
            for idx in indexes:
                if idx >= len(items):
                    continue
                item = items[idx] or {}
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                price_label = self._catalog_price_label(item.get("price", item.get("final_price")))
                details = self._catalog_detail_names(item.get("details") or [])
                plans.append(
                    {
                        "position": len(plans) + 1,
                        "segment": current_segment,
                        "name": name,
                        "normalized_name": normalize_text(name),
                        "mbps": str(item.get("mbps") or "").strip() or None,
                        "mbps_value": self._catalog_bandwidth_value(item.get("mbps")),
                        "price": price_label,
                        "price_value": self._catalog_price_value(price_label),
                        "details": details,
                    }
                )
        return plans

    def _store_catalog_context(self, sales_state: dict, *, data: dict, segment: str | None = None) -> None:
        """Almacena contexto de catalog."""
        plans = self._display_catalog_items(data, segment=segment)
        self._store_catalog_context_from_plans(sales_state, plans=plans, segment=segment)

    @staticmethod
    def _store_catalog_context_from_plans(
        sales_state: dict,
        *,
        plans: list[dict[str, Any]],
        segment: str | None = None,
    ) -> None:
        """Almacena catalog contexto from plans."""
        if not plans:
            SalesCommercialHelpersMixin._clear_catalog_context(sales_state)
            return
        normalized_plans: list[dict[str, Any]] = []
        for position, plan in enumerate(plans, start=1):
            item = dict(plan or {})
            item["position"] = position
            normalized_plans.append(item)
        sales_state["catalog_context"] = {
            "segment": segment,
            "plans": normalized_plans,
        }
        sales_state["selected_catalog_plan"] = None

    @staticmethod
    def _clear_catalog_context(sales_state: dict) -> None:
        """Elimina el catálogo cacheado y la selección actual del cliente."""
        sales_state["catalog_context"] = None
        sales_state["selected_catalog_plan"] = None

    @staticmethod
    def _find_catalog_plan_by_reference(sales_state: dict, user_message: str) -> dict[str, Any] | None:
        """Devuelve el reference find catalog plan by."""
        context = sales_state.get("catalog_context") or {}
        plans = [item for item in (context.get("plans") or []) if isinstance(item, dict)]
        raw_message = str(user_message or "").strip()
        normalized = normalize_text(raw_message)
        if not plans or not normalized:
            return None

        selected = sales_state.get("selected_catalog_plan")
        if normalized in {"ese", "este", "ese mismo", "este mismo"} and isinstance(selected, dict):
            return selected

        if contains_any_phrase(normalized, ("mas barato", "más barato", "barato", "economico", "económico")):
            priced = [plan for plan in plans if plan.get("price_value") is not None]
            if priced:
                return min(priced, key=lambda item: item.get("price_value") or 0.0)

        for alias, position in CATALOG_ORDINAL_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", normalized):
                for plan in plans:
                    if plan.get("position") == position:
                        return plan

        name_matches = [plan for plan in plans if str(plan.get("normalized_name") or "") and str(plan.get("normalized_name")) in normalized]
        if len(name_matches) == 1:
            return name_matches[0]

        bandwidth_matches: list[dict[str, Any]] = []
        for raw_bandwidth in CATALOG_BANDWIDTH_PATTERN.findall(normalized):
            try:
                bandwidth_value = int(raw_bandwidth)
            except (TypeError, ValueError):
                continue
            matched = [plan for plan in plans if plan.get("mbps_value") == bandwidth_value]
            if len(matched) == 1:
                bandwidth_matches.append(matched[0])
        if len(bandwidth_matches) == 1:
            return bandwidth_matches[0]

        price_matches: list[dict[str, Any]] = []
        for raw_price in CATALOG_PRICE_PATTERN.findall(raw_message):
            parsed_price = SalesCommercialHelpersMixin._catalog_price_value(raw_price)
            if parsed_price is None:
                continue
            matched = [
                plan
                for plan in plans
                if plan.get("price_value") is not None and abs(float(plan.get("price_value")) - parsed_price) < 0.011
            ]
            if len(matched) == 1:
                price_matches.append(matched[0])
        for whole, cents in CATALOG_SPACED_PRICE_PATTERN.findall(normalized):
            parsed_price = SalesCommercialHelpersMixin._catalog_price_value(f"{whole}.{cents}")
            if parsed_price is None:
                continue
            matched = [
                plan
                for plan in plans
                if plan.get("price_value") is not None and abs(float(plan.get("price_value")) - parsed_price) < 0.011
            ]
            if len(matched) == 1:
                price_matches.append(matched[0])
        unique_price_matches: dict[tuple[Any, Any], dict[str, Any]] = {}
        for match in price_matches:
            unique_price_matches[(match.get("position"), match.get("normalized_name"))] = match
        if len(unique_price_matches) == 1:
            return next(iter(unique_price_matches.values()))

        return None

    @staticmethod
    def _set_recommended_plan_from_catalog(sales_state: dict, plan: dict[str, Any]) -> None:
        """Promueve un plan del catálogo a la ranura de plan recomendado."""
        sales_state["selected_catalog_plan"] = dict(plan)
        sales_state["recommended_plan"] = {
            "name": plan.get("name"),
            "mbps": plan.get("mbps"),
            "price": plan.get("price"),
            "segment": plan.get("segment") or "residential",
            "details": list(plan.get("details") or []),
            "source": "catalog",
        }

    def _build_catalog_plan_selection_message(self, *, sales_state: dict, plan: dict[str, Any], user_message: str) -> str:
        """Construye mensaje catalog plan selection a partir del contexto disponible."""
        del user_message
        self._set_recommended_plan_from_catalog(sales_state, plan)
        profile = dict(sales_state.get("recommendation_profile") or {})
        if not profile.get("segment") and plan.get("segment"):
            profile["segment"] = plan.get("segment")
            sales_state["recommendation_profile"] = profile
        prefix = f"Sí, el de **${plan.get('price') or '-'}** es **{plan.get('name') or 'ese plan'}**"
        if plan.get("mbps"):
            prefix = f"{prefix} de **{plan.get('mbps')} Mbps**."
        else:
            prefix = f"{prefix}."
        context_plans = [item for item in ((sales_state.get('catalog_context') or {}).get('plans') or []) if isinstance(item, dict)]
        priced = [item for item in context_plans if item.get("price_value") is not None]
        if priced and plan.get("price_value") is not None:
            cheapest = min(priced, key=lambda item: item.get("price_value") or 0.0)
            if cheapest.get("name") == plan.get("name"):
                segment_label = "hogar" if plan.get("segment") != "pymes" else "pyme"
                prefix = f"{prefix}\n\nDentro de los planes {segment_label}, es la opción más accesible que tengo ahora."
        followup = build_recommended_plan_followup_prompt(
            sales_state.get("recommended_plan") or {},
            sales_state.get("recommendation_profile") or {},
        )
        return f"{prefix}\n\n{followup}".strip()

    def _format_plan_catalog(self, data: dict, *, segment: str | None = None) -> str:
        """Da formato a plan catalog para presentarlo de forma clara."""
        residential = self.promotions.format_gonectados_combos(data)
        pymes = self.promotions.format_pymes(data)
        has_residential = bool(residential and residential.strip() != "PLANES GONECTADOS:")
        has_pymes = bool(pymes and pymes.strip() != "PLANES PYMES:")

        if segment == "residential":
            if not has_residential:
                return "Por ahora no tengo disponible el catálogo de planes hogar."
            return "Estos son los planes hogar que tengo ahora:\n\n" + residential

        if segment == "pymes":
            if not has_pymes:
                return "Por ahora no tengo disponible el catálogo de planes pymes."
            return "Estos son los planes pymes que tengo ahora:\n\n" + pymes

        sections: list[str] = []
        if has_residential:
            sections.append(residential)
        if has_pymes:
            sections.append(pymes)
        if not sections:
            return "Por ahora no tengo disponible el catálogo de planes."
        return "Estos son los planes que tengo disponibles ahora:\n\n" + "\n\n".join(sections)

    @staticmethod
    def _format_display_plan_block(plan: dict[str, Any]) -> str:
        """Da formato a display plan block para presentarlo de forma clara."""
        name = str(plan.get("name") or "-").strip()
        mbps = str(plan.get("mbps") or "-").strip()
        price = str(plan.get("price") or "-").strip()
        details = [str(item).strip() for item in (plan.get("details") or []) if str(item).strip()]
        lines = [
            f"**{name}**",
            f"- **Velocidad:** **{mbps} Mbps**",
            f"- **Precio + IMP:** **${price}**",
        ]
        if details:
            lines.append(f"- **Incluye:** {', '.join(details)}")
        return "\n".join(lines)

    def _format_filtered_plan_catalog(self, *, plans: list[dict[str, Any]], segment: str | None, intro: str) -> str:
        """Da formato a filtered plan catalog para presentarlo de forma clara."""
        if not plans:
            return intro
        segment_label = "hogar" if segment != "pymes" else "pyme"
        heading = f"Estas son las opciones {segment_label} con más megas que tengo ahora:"
        blocks = [self._format_display_plan_block(plan) for plan in plans]
        return "\n\n".join(part for part in (intro, heading, *blocks) if part)

    async def _handle_higher_speed_catalog_without_registration(
        self,
        *,
        sales_state: dict,
        user_message: str,
    ) -> AgentResult:
        """Responde cuando el cliente pide más megas sin haber pasado por registro."""
        segment = (
            (sales_state.get("recommendation_profile") or {}).get("segment")
            or (sales_state.get("recommended_plan") or {}).get("segment")
            or sales_state.get("commercial_catalog_segment")
        )
        current_bandwidth = self._catalog_bandwidth_value((sales_state.get("recommended_plan") or {}).get("mbps"))
        data = await self._fetch_catalog_or_empty(recipient="info_only")
        plans = self._display_catalog_items(data, segment=segment)
        higher_plans = [
            plan
            for plan in plans
            if plan.get("mbps_value") is not None
            and current_bandwidth is not None
            and int(plan.get("mbps_value")) > int(current_bandwidth)
        ]
        if not higher_plans:
            plan_name = str(((sales_state.get("recommended_plan") or {}).get("name")) or "ese plan").strip()
            plan_mbps = str(((sales_state.get("recommended_plan") or {}).get("mbps")) or "").strip()
            segment_label = "hogar" if segment != "pymes" else "pyme"
            speed_text = f" de {plan_mbps} Mbps" if plan_mbps else ""
            message = (
                f"Por ahora, dentro de los planes {segment_label}, no tengo una opción con más megas que *{plan_name}*{speed_text}. "
                "Si lo prefiere, avanzamos con ese plan o le comparto todas las opciones para comparar."
            )
            response_plan = self._sales_response_plan(
                sales_state=sales_state,
                message=message,
                conversation_state="recommended_plan_higher_speed_unavailable",
                reply_goal="responder con claridad cuando no haya un upgrade por encima del plan recomendado",
                next_step="esperar_si_avanza_o_pide_catalogo_completo",
                followup_prompt=build_recommended_plan_followup_prompt(
                    sales_state.get("recommended_plan") or {},
                    sales_state.get("recommendation_profile") or {},
                ),
                hypothesis="no_higher_speed_plan_available",
            )
            return self._respond(
                sales_state=sales_state,
                user_message=user_message,
                message=message,
                intent="commercial",
                metadata=self._recommendation_metadata(sales_state),
                response_plan=response_plan,
            )

        self._store_catalog_context_from_plans(sales_state, plans=higher_plans, segment=segment)
        self._pause_commercial_capture(sales_state)
        sales_state["pending_intent"] = None
        sales_state["commercial_catalog_segment"] = segment
        sales_state["commercial_registration_declined"] = True
        final_message = self._format_filtered_plan_catalog(
            plans=higher_plans,
            segment=segment,
            intro="Sí, tengo opciones con más megas. Te paso las que están por encima de ese plan para que compares.",
        )
        response_plan = self._sales_response_plan(
            sales_state=sales_state,
            message=final_message,
            conversation_state="catalog_higher_speed_only",
            reply_goal="mostrar opciones de mayor velocidad sin reiniciar la conversación comercial",
            next_step="esperar_si_elige_una_opcion_o_quiere_avanzar",
            hypothesis="customer_requested_higher_speed_options",
        )
        return self._respond(
            sales_state=sales_state,
            user_message=user_message,
            message=final_message,
            intent="commercial",
            metadata={
                "lead": sales_state.get("lead") or {},
                "crm_lead_created": False,
                "commercial_registration_declined": True,
            },
            response_plan=response_plan,
        )

    async def _handle_agencies_lookup(self, *, sales_state: dict, user_message: str) -> AgentResult:
        """Maneja la busqueda de agencias y avanza el flujo."""
        lead = sales_state.get("lead") or {}
        city = lead.get("city")
        province = lead.get("province")
        if province and not city:
            rows = await self.agencies.by_province(province.upper())
        else:
            rows = await self.agencies.by_city((city or "").upper())
            if not rows and province:
                rows = await self.agencies.by_province(province.upper())
            if not rows and city:
                rows = await self.agencies.by_province(city.upper())

        sales_state["awaiting_agency_location"] = False
        sales_state["pending_intent"] = None
        if not rows:
            message = "Por ahora no encuentro agencias registradas en esa ciudad o provincia."
            response_plan = self._sales_response_plan(
                sales_state=sales_state,
                message=message,
                conversation_state="agencies_not_found",
                reply_goal="explicar con claridad cuando no encontré agencias para esa zona",
                next_step="esperar_otra_ciudad_o_provincia",
                followup_prompt="Si lo prefiere, indíqueme otra ciudad o provincia y lo reviso.",
                hypothesis="no_agencies_for_requested_location",
            )
            return self._respond(
                sales_state=sales_state,
                user_message=user_message,
                message=message,
                intent="agencies",
                metadata={"agencias": []},
                response_plan=response_plan,
            )

        lines = ["Estas son las agencias encontradas por esa zona:"]
        for row in rows[:8]:
            lines += [
                f"Agencia: {row.get('agencia') or row.get('nombre') or 'Agencia'}",
                f"Ciudad: {row.get('ciudad', '') or '-'}",
                f"Dirección: {row.get('direccion', row.get('dirección', '')) or '-'}",
                f"Horarios: {row.get('horarios', '') or '-'}",
                "",
            ]
        message = "\n".join(lines).strip()
        response_plan = self._sales_response_plan(
            sales_state=sales_state,
            message=message,
            conversation_state="agencies_result",
            reply_goal="entregar agencias encontradas como si un asesor especializado ubicara opciones cercanas",
            next_step="esperar_si_quiere_otra_zona_o_otra_ayuda",
            hypothesis="agencies_found_for_location",
        )
        return self._respond(
            sales_state=sales_state,
            user_message=user_message,
            message=message,
            intent="agencies",
            metadata={"agencias": rows[:8], "lead": lead},
            response_plan=response_plan,
        )

    @staticmethod
    def _preferred_crm_street(lead: dict) -> str | None:
        """Devuelve el street preferred crm."""
        if not isinstance(lead, dict):
            return None
        precise_location = lead.get("latitude") is not None and lead.get("longitude") is not None
        if precise_location and lead.get("address"):
            return lead.get("address")
        return lead.get("street") or lead.get("address")

    async def _save_supported_city_answer(self, sales_state: dict, text: str) -> str:
        """Guarda supported city answer."""
        cleaned = norm(text)
        if not cleaned:
            return self.CITY_REPLY_INVALID
        if _looks_like_precise_address(cleaned):
            return self.CITY_REPLY_STREETLIKE

        location = _extract_location_from_text(cleaned)
        city = location.get("city")
        if not city and "provincia" not in cleaned.lower():
            city = location.get("province")
        if not city:
            return self.CITY_REPLY_INVALID

        rows = await self.agencies.by_city(city.upper())
        if not rows:
            return self.CITY_REPLY_UNSUPPORTED

        lead = sales_state.setdefault("lead", {})
        first = rows[0] or {}
        lead["city"] = _title_case(first.get("ciudad") or city)
        province = first.get("provincia") or location.get("province")
        if province:
            lead["province"] = _title_case(province)
        sales_state["commercial_registration_declined"] = False
        sales_state["fresh_location"] = True
        return self.CITY_REPLY_SAVED

    async def _save_supported_city_from_location(self, sales_state: dict) -> bool:
        """Guarda supported city from location."""
        lead = sales_state.setdefault("lead", {})
        city = str(lead.get("city") or "").strip()
        if not city:
            return False
        rows = await self.agencies.by_city(city.upper())
        if not rows:
            lead.pop("city", None)
            return False
        first = rows[0] or {}
        lead["city"] = _title_case(first.get("ciudad") or city)
        province = str(first.get("provincia") or lead.get("province") or "").strip()
        if province:
            lead["province"] = _title_case(province)
        sales_state["commercial_registration_declined"] = False
        sales_state["fresh_location"] = True
        return True

    async def _create_crm_lead(self, sales_state: dict) -> str:
        """Devuelve el lead create crm."""
        lead = sales_state.get("lead") or {}
        payload = {
            "type": "lead",
            "partner_name": lead.get("partner_name"),
            "city": lead.get("city"),
            "street": self._preferred_crm_street(lead),
            "phone": lead.get("phone"),
            "latitude": lead.get("latitude"),
            "longitude": lead.get("longitude"),
        }
        result = await self.crm.create_lead(payload)
        sales_state["crm_lead_result"] = result
        if result.get("status") == "created":
            sales_state["crm_lead_created"] = True
            return CRM_CREATED_MSG
        return CRM_PENDING_MSG

    async def _ensure_commercial_followup(self, *, sales_state: dict, channel: str, recipient: str, cedula: str | None) -> None:
        """Deja la sesión preparada para continuar el seguimiento comercial."""
        if sales_state.get("commercial_handoff_requested"):
            return
        try:
            await self.handoff.escalate_new_client(
                channel=channel,
                recipient=recipient,
                cedula=cedula,
                origen=self.settings.info_origen,
                group="iainfo",
            )
            sales_state["commercial_handoff_requested"] = True
        except SALES_EXTERNAL_ERRORS:
            logger.exception("commercial_handoff_failed recipient=%s channel=%s", recipient, channel)

    async def _fetch_catalog_or_empty(self, *, recipient: str) -> dict:
        """Consulta el catálogo o devuelve una estructura vacía si no se puede obtener."""
        try:
            return await self.promotions.fetch_catalog()
        except SALES_EXTERNAL_ERRORS:
            logger.exception("promotions_fetch_failed recipient=%s", recipient)
            return {}

    async def _handle_commercial_catalog(
        self,
        *,
        sales_state: dict,
        user_message: str,
        channel: str,
        recipient: str,
        cedula: str | None,
        prefix_message: str | None = None,
    ) -> AgentResult:
        """Responde con catálogo, selección de plan o seguimiento del flujo comercial."""
        sales_state["pending_intent"] = None
        confirmation = CRM_CREATED_MSG if sales_state.get("crm_lead_created") else await self._create_crm_lead(sales_state)
        await self._ensure_commercial_followup(
            sales_state=sales_state,
            channel=channel,
            recipient=recipient,
            cedula=cedula,
        )
        parts = [confirmation]
        if sales_state.get("commercial_catalog_requested"):
            data = await self._fetch_catalog_or_empty(recipient=recipient)
            self._store_catalog_context(
                sales_state,
                data=data,
                segment=sales_state.get("commercial_catalog_segment"),
            )
            parts.append(self._format_plan_catalog(data, segment=sales_state.get("commercial_catalog_segment")))
        parts.append(COMMERCIAL_FOLLOWUP_MSG)
        if prefix_message:
            parts.insert(0, prefix_message)
        sales_state["awaiting_crm_field"] = None
        sales_state["commercial_catalog_requested"] = False
        self._clear_commercial_info_choice(sales_state)
        self._reset_commercial_recommendation(sales_state)
        final_message = "\n\n".join(part for part in parts if part)
        response_plan = self._sales_response_plan(
            sales_state=sales_state,
            message=final_message,
            conversation_state="registration_catalog_complete",
            reply_goal="confirmar el registro comercial y dejar el catálogo como referencia útil",
            next_step="esperar_contacto_comercial",
            hypothesis="commercial_lead_registered",
        )
        return self._respond(
            sales_state=sales_state,
            user_message=user_message,
            message=final_message,
            intent="commercial",
            metadata={"lead": sales_state.get("lead") or {}, "crm_lead_created": sales_state.get("crm_lead_created")},
            response_plan=response_plan,
        )

    def _final_registration_confirmation(self, sales_state: dict, confirmation: str) -> str:
        """Cierra la captura CRM con una confirmación clara para el cliente."""
        plan_name = str(((sales_state.get("recommended_plan") or {}).get("name")) or "").strip()
        if not plan_name:
            return confirmation
        if confirmation == CRM_CREATED_MSG:
            return f"Listo, ya dejé registrada su solicitud para *{plan_name}*."
        if confirmation == CRM_PENDING_MSG:
            return f"Listo, ya tengo sus datos para seguimiento con *{plan_name}*."
        return confirmation

    async def _finalize_commercial_registration(
        self,
        *,
        sales_state: dict,
        user_message: str,
        channel: str,
        recipient: str,
        cedula: str | None,
        prefix_message: str | None = None,
    ) -> AgentResult:
        """Termina la captura comercial y decide si el lead ya puede pasar a CRM."""
        sales_state["pending_intent"] = None
        confirmation = CRM_CREATED_MSG if sales_state.get("crm_lead_created") else await self._create_crm_lead(sales_state)
        confirmation = self._final_registration_confirmation(sales_state, confirmation)
        await self._ensure_commercial_followup(
            sales_state=sales_state,
            channel=channel,
            recipient=recipient,
            cedula=cedula,
        )
        parts = [confirmation, COMMERCIAL_FOLLOWUP_MSG]
        if prefix_message:
            parts.insert(0, prefix_message)
        sales_state["awaiting_crm_field"] = None
        sales_state["commercial_catalog_requested"] = False
        self._clear_commercial_info_choice(sales_state)
        self._reset_commercial_recommendation(sales_state)
        final_message = "\n\n".join(part for part in parts if part)
        response_plan = self._sales_response_plan(
            sales_state=sales_state,
            message=final_message,
            conversation_state="registration_complete",
            reply_goal="confirmar que la solicitud quedó tomada y marcar el siguiente paso humano",
            next_step="esperar_contacto_comercial",
            hypothesis="commercial_lead_registered",
        )
        return self._respond(
            sales_state=sales_state,
            user_message=user_message,
            message=final_message,
            intent="commercial",
            metadata={"lead": sales_state.get("lead") or {}, "crm_lead_created": sales_state.get("crm_lead_created")},
            response_plan=response_plan,
        )

    def _handle_general_intent(self, *, sales_state: dict, user_message: str, intent: str) -> AgentResult:
        """Maneja general intent y avanza el flujo."""
        rules = {
            "contacto": "Si desea llamar, el número del Contact Center es +593962925555.",
            "payments": PAYMENTS_REDIRECT_MSG,
            "payment_methods": PAYMENTS_REDIRECT_MSG,
            "pago_mensual": PAYMENTS_REDIRECT_MSG,
        }
        sales_state["pending_intent"] = None
        message = rules[intent]
        response_plan = self._sales_response_plan(
            sales_state=sales_state,
            message=message,
            conversation_state=f"general_{intent}",
            reply_goal="responder una consulta puntual sin perder el tono de asesor especializado",
            next_step="esperar_nueva_consulta",
            hypothesis=f"customer_requested_{intent}",
        )
        return self._respond(
            sales_state=sales_state,
            user_message=user_message,
            message=message,
            intent=intent,
            actions=MAIN_MENU_ACTIONS if intent in {"payments", "payment_methods", "pago_mensual"} else None,
            response_plan=response_plan,
        )

    @staticmethod
    def _pause_commercial_capture(sales_state: dict) -> None:
        """Suspende temporalmente la captura comercial mientras se atiende otra ruta."""
        sales_state["awaiting_crm_field"] = None
        sales_state["commercial_catalog_requested"] = False
        sales_state["commercial_catalog_segment"] = None
        sales_state["commercial_registration_declined"] = False
        sales_state["awaiting_info_choice"] = False
        SalesCommercialHelpersMixin._reset_commercial_recommendation(sales_state)

    async def _handle_commercial_catalog_without_registration(
        self,
        *,
        sales_state: dict,
        user_message: str,
        prefix_message: str | None = None,
    ) -> AgentResult:
        """Responde al catálogo cuando todavía no conviene abrir la captura CRM."""
        segment = sales_state.get("commercial_catalog_segment")
        parts = [prefix_message or COMMERCIAL_INFO_ONLY_MSG]
        if sales_state.get("commercial_catalog_requested"):
            data = await self._fetch_catalog_or_empty(recipient="info_only")
            self._store_catalog_context(
                sales_state,
                data=data,
                segment=segment,
            )
            parts.append(self._format_plan_catalog(data, segment=segment))
        else:
            self._clear_catalog_context(sales_state)
        self._pause_commercial_capture(sales_state)
        sales_state["pending_intent"] = None
        sales_state["commercial_catalog_segment"] = segment or ((sales_state.get("catalog_context") or {}).get("segment"))
        sales_state["commercial_registration_declined"] = True
        final_message = "\n\n".join(part for part in parts if part)
        response_plan = self._sales_response_plan(
            sales_state=sales_state,
            message=final_message,
            conversation_state="catalog_only",
            reply_goal="entregar información comercial sin forzar el registro",
            next_step="esperar_nueva_pregunta_o_recomendacion",
            hypothesis="customer_wants_catalog_only",
        )
        return self._respond(
            sales_state=sales_state,
            user_message=user_message,
            message=final_message,
            intent="commercial",
            metadata={
                "lead": sales_state.get("lead") or {},
                "crm_lead_created": False,
                "commercial_registration_declined": True,
            },
            response_plan=response_plan,
        )

    async def _handle_crm_capture_override(
        self,
        *,
        sales_state: dict,
        user_message: str,
        text: str,
        intent: str,
    ) -> AgentResult | None:
        """Sobrescribe la captura CRM cuando el turno activo exige otra acción."""
        if intent == "agencies":
            self._pause_commercial_capture(sales_state)
            sales_state["pending_intent"] = "agencies"
            self._reset_agency_location_scope(sales_state)
            await self._ingest_text_location(sales_state, _strip_agency_words(text))
            if not self._has_general_location(sales_state):
                sales_state["awaiting_agency_location"] = True
                response_plan = self._sales_response_plan(
                    sales_state=sales_state,
                    message=AGENCY_PROMPT,
                    conversation_state="agencies_location_prompt",
                    reply_goal="pedir la ubicación de forma natural para buscar agencias",
                    next_step="esperar_ciudad_o_provincia",
                    followup_prompt=AGENCY_PROMPT,
                    hypothesis="needs_agency_location",
                )
                return self._respond(
                    sales_state=sales_state,
                    user_message=user_message,
                    message=AGENCY_PROMPT,
                    intent="agencies",
                    response_plan=response_plan,
                )
            return await self._handle_agencies_lookup(sales_state=sales_state, user_message=user_message)

        if intent == "menu":
            self._pause_commercial_capture(sales_state)
            sales_state["pending_intent"] = None
            response_plan = self._sales_response_plan(
                sales_state=sales_state,
                message=MENU,
                conversation_state="menu",
                reply_goal="mostrar las opciones principales sin sonar a menú robótico",
                next_step="esperar_seleccion",
                hypothesis="customer_requested_menu",
            )
            return self._respond(
                sales_state=sales_state,
                user_message=user_message,
                message=MENU,
                intent="menu",
                response_plan=response_plan,
            )

        if intent in {"payments", "payment_methods", "pago_mensual", "contacto"}:
            self._pause_commercial_capture(sales_state)
            sales_state["awaiting_agency_location"] = False
            return self._handle_general_intent(sales_state=sales_state, user_message=user_message, intent=intent)

        return None

    def _recommendation_metadata(self, sales_state: dict) -> dict:
        """Devuelve la metadata recommendation."""
        metadata = {
            "lead": sales_state.get("lead") or {},
            "commercial_registration_declined": bool(sales_state.get("commercial_registration_declined")),
            "recommendation_profile": dict(sales_state.get("recommendation_profile") or {}),
        }
        if sales_state.get("recommended_plan"):
            metadata["recommended_plan"] = dict(sales_state.get("recommended_plan") or {})
        return metadata

    async def _start_commercial_recommendation(
        self,
        *,
        sales_state: dict,
        user_message: str,
        profile_updates: dict | None = None,
        prefix_message: str | None = None,
    ) -> AgentResult:
        """Inicia la recomendación guiada a partir del perfil del cliente."""
        sales_state["pending_intent"] = "commercial"
        sales_state["recommended_plan"] = None
        self._clear_commercial_info_choice(sales_state)
        merged_profile = await self._merge_recommendation_profile(
            sales_state=sales_state,
            user_message=user_message,
            current_field=sales_state.get("awaiting_recommendation_field"),
        )
        if profile_updates:
            merged_profile.update({key: value for key, value in profile_updates.items() if value is not None})
        sales_state["recommendation_profile"] = merged_profile
        sales_state["commercial_catalog_segment"] = merged_profile.get("segment") or sales_state.get("commercial_catalog_segment")
        sales_state["commercial_registration_declined"] = True
        next_field = next_recommendation_field(merged_profile)
        if next_field is None:
            return await self._finalize_commercial_recommendation(
                sales_state=sales_state,
                user_message=user_message,
            )
        sales_state["awaiting_recommendation_field"] = next_field
        seed_field = None
        if sales_state.get("awaiting_recommendation_field") != "segment" and merged_profile.get("segment"):
            seed_field = "segment"
        question = (
            build_recommendation_step_message(
                current_field=seed_field,
                next_field=next_field,
                profile=merged_profile,
            )
            if seed_field
            else recommendation_question(next_field, merged_profile)
        )
        message = f"{prefix_message}\n\n{question}" if prefix_message else f"{COMMERCIAL_RECOMMENDATION_MSG} {question}"
        response_plan = self._sales_response_plan(
            sales_state=sales_state,
            message=message,
            conversation_state="recommendation_question",
            reply_goal="hacer una asesoría comercial guiada con un asesor especializado sin perder continuidad",
            next_step=f"capturar_{next_field}",
            followup_prompt=question,
            hypothesis="collect_recommendation_profile",
        )
        return self._respond(
            sales_state=sales_state,
            user_message=user_message,
            message=message,
            intent="commercial",
            metadata=self._recommendation_metadata(sales_state),
            response_plan=response_plan,
        )

    async def _finalize_commercial_recommendation(
        self,
        *,
        sales_state: dict,
        user_message: str,
    ) -> AgentResult:
        """Cierra la recomendación y fija el plan sugerido en el estado."""
        profile = sales_state.get("recommendation_profile") or {}
        segment = profile.get("segment")
        if segment:
            sales_state["commercial_catalog_segment"] = segment
        data = await self._fetch_catalog_or_empty(recipient="recommendation")
        plan = recommend_plan(data, profile)
        sales_state["awaiting_recommendation_field"] = None
        if not plan:
            sales_state["commercial_catalog_requested"] = True
            return await self._handle_commercial_catalog_without_registration(
                sales_state=sales_state,
                user_message=user_message,
                prefix_message="Por ahora no pude calcular una recomendación puntual, pero sí le comparto las opciones disponibles.",
            )
        sales_state["recommended_plan"] = {
            "name": plan.get("name"),
            "mbps": plan.get("mbps"),
            "price": plan.get("price", plan.get("final_price")),
            "segment": segment or "residential",
        }
        message = build_recommendation_message(plan, profile)
        metadata = self._recommendation_metadata(sales_state)
        response_plan = self._sales_response_plan(
            sales_state=sales_state,
            message=message,
            conversation_state="recommendation_result",
            reply_goal="recomendar un plan puntual y dejar claro cómo avanzar",
            next_step="esperar_si_avanza_o_compara",
            followup_prompt=build_recommended_plan_followup_prompt(
                sales_state.get("recommended_plan") or {},
                sales_state.get("recommendation_profile") or {},
            ),
            hypothesis="recommended_best_fit_plan",
        )
        return self._respond(
            sales_state=sales_state,
            user_message=user_message,
            message=message,
            intent="commercial",
            metadata=metadata,
            response_plan=response_plan,
        )

    async def _continue_commercial_recommendation(
        self,
        *,
        sales_state: dict,
        user_message: str,
        text: str,
        wants_full_catalog: bool = False,
    ) -> AgentResult:
        """Continúa la conversación de recomendación por el siguiente campo pendiente."""
        if wants_full_catalog:
            sales_state["commercial_catalog_requested"] = True
            segment = (sales_state.get("recommendation_profile") or {}).get("segment")
            if segment:
                sales_state["commercial_catalog_segment"] = segment
            return await self._handle_commercial_catalog_without_registration(
                sales_state=sales_state,
                user_message=user_message,
                prefix_message="Le comparto todas las opciones que tengo ahora.",
            )

        current_field = sales_state.get("awaiting_recommendation_field")
        merged_profile = await self._merge_recommendation_profile(
            sales_state=sales_state,
            user_message=text,
            current_field=current_field,
        )
        sales_state["recommendation_profile"] = merged_profile
        segment = merged_profile.get("segment")
        if segment:
            sales_state["commercial_catalog_segment"] = segment
        next_field = next_recommendation_field(merged_profile)
        if next_field is None:
            return await self._finalize_commercial_recommendation(
                sales_state=sales_state,
                user_message=user_message,
            )
        sales_state["awaiting_recommendation_field"] = next_field
        captured_current_field = current_field and merged_profile.get(current_field)
        if captured_current_field:
            message = build_recommendation_step_message(
                current_field=current_field,
                next_field=next_field,
                profile=merged_profile,
            )
        else:
            message = build_recommendation_retry_message(current_field, merged_profile)
        response_plan = self._sales_response_plan(
            sales_state=sales_state,
            message=message,
            conversation_state="recommendation_question",
            reply_goal="seguir afinando la recomendación con base en lo que ya respondió el cliente",
            next_step=f"capturar_{next_field}",
            followup_prompt=recommendation_question(next_field, merged_profile),
            hypothesis="collect_recommendation_profile",
        )
        return self._respond(
            sales_state=sales_state,
            user_message=user_message,
            message=message,
            intent="commercial",
            metadata=self._recommendation_metadata(sales_state),
            response_plan=response_plan,
        )

    async def _continue_agencies_followup(self, *, sales_state: dict, user_message: str, text: str) -> AgentResult:
        """Sigue el hilo de agencias con la información que todavía falta."""
        self._reset_agency_location_scope(sales_state)
        await self._ingest_text_location(sales_state, _strip_agency_words(text))
        if not self._has_general_location(sales_state):
            sales_state["awaiting_agency_location"] = True
            response_plan = self._sales_response_plan(
                sales_state=sales_state,
                message=AGENCY_PROMPT,
                conversation_state="agencies_location_prompt",
                reply_goal="pedir la ubicación para buscar agencias sin perder continuidad",
                next_step="esperar_ciudad_o_provincia",
                followup_prompt=AGENCY_PROMPT,
                hypothesis="needs_agency_location",
            )
            return self._respond(
                sales_state=sales_state,
                user_message=user_message,
                message=AGENCY_PROMPT,
                intent="agencies",
                response_plan=response_plan,
            )
        return await self._handle_agencies_lookup(sales_state=sales_state, user_message=user_message)

    async def _continue_crm_capture(
        self,
        *,
        sales_state: dict,
        user_message: str,
        channel: str,
        recipient: str,
        cedula: str | None,
        prefix_message: str | None = None,
    ) -> AgentResult:
        """Sigue la captura CRM con el siguiente dato pendiente."""
        next_field = self._next_crm_field(sales_state)
        self._clear_commercial_info_choice(sales_state)
        if next_field:
            sales_state["awaiting_crm_field"] = next_field
            question = CRM_QUESTIONS[next_field]
            lead = sales_state.get("lead") or {}
            if next_field == "coordinates" and (lead.get("street") or lead.get("address")) and not self._has_precise_location(sales_state):
                question = LOCATION_FALLBACK_PROMPT
            text = f"{prefix_message}\n\n{question}" if prefix_message else question
            response_plan = self._sales_response_plan(
                sales_state=sales_state,
                message=text,
                conversation_state="crm_capture",
                reply_goal="pedir el siguiente dato comercial sin romper el tono humano",
                next_step=f"capturar_{next_field}",
                followup_prompt=question,
                hypothesis="collect_registration_data",
            )
            return self._respond(
                sales_state=sales_state,
                user_message=user_message,
                message=text,
                intent="commercial",
                metadata={"lead": sales_state.get("lead") or {}},
                response_plan=response_plan,
            )
        return await self._finalize_commercial_registration(
            sales_state=sales_state,
            user_message=user_message,
            channel=channel,
            recipient=recipient,
            cedula=cedula,
            prefix_message=prefix_message,
        )

    def _save_crm_answer(self, sales_state: dict, field: str, text: str) -> bool:
        """Guarda crm answer."""
        cleaned = norm(text)
        if not cleaned:
            return False

        lead = sales_state.setdefault("lead", {})
        if field == "partner_name":
            lead[field] = _title_case(cleaned)
            sales_state["commercial_registration_declined"] = False
            return True
        if field == "city":
            location = _extract_location_from_text(cleaned)
            city = location.get("city")
            if not city and "provincia" not in cleaned.lower():
                # When the user is answering the city question, inputs like "Loja"
                # can legitimately be both city and province. Accept the city value
                # from that province match instead of looping on the same question.
                city = location.get("province")
            if not city:
                return False
            lead["city"] = _title_case(city)
            province = location.get("province")
            if province and not lead.get("province"):
                lead["province"] = _title_case(province)
            sales_state["commercial_registration_declined"] = False
            return True
        if field == "street":
            street = _street_candidate(cleaned) or lead.get("address")
            if not street:
                return False
            lead[field] = street
            sales_state["commercial_registration_declined"] = False
            return True
        if field == "phone":
            if not self._looks_like_phone(cleaned):
                return False
            lead[field] = cleaned
            sales_state["commercial_registration_declined"] = False
            return True
        return False

    def _save_crm_answer_from_location(self, sales_state: dict, field: str) -> bool:
        """Guarda crm answer from location."""
        lead = sales_state.setdefault("lead", {})
        if field == "city":
            return bool(lead.get("city"))
        if field == "street":
            if lead.get("address"):
                lead["street"] = lead["address"]
                return True
        if field == "coordinates":
            return self._has_precise_location(sales_state)
        return False
