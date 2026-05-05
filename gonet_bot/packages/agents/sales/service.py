"""Agente comercial principal."""

import logging

from packages.agents.handoff.service import create_direct_handoff_result
from packages.agents.sales.flow_helpers import (
    AGENCY_PROMPT,
    COMMERCIAL_RECOMMENDATION_INFO_ONLY_MSG,
    CRM_QUESTIONS,
    DISCOVERY_MSG,
    INFO_INTRO,
    MENU,
    OUT_OF_SCOPE_MSG,
    SALES_EXTERNAL_ERRORS,
    SalesFlowHelpersMixin,
)
from packages.agents.sales.recommendation_utils import (
    build_recommended_plan_followup_prompt,
    extract_recommendation_slot_updates,
    recommendation_question,
)
from packages.integrations.agencies_repo import AgenciesRepo, asyncpg as agencies_asyncpg
from packages.integrations.geocoder import GeocoderClient
from packages.integrations.odoo_chat import OdooChatClient
from packages.integrations.odoo_crm import OdooCRMClient
from packages.integrations.openai_client import OpenAIClient
from packages.integrations.promotions_api import PromotionsAPI
from packages.shared.config import get_settings
from packages.shared.sales_intents import analyze_sales_message, detect_sales_intent
from packages.shared.schemas import AgentResult, InboundMessage, SessionState
from packages.shared.turn_interpreter import ActiveFlowTurnInterpreter

logger = logging.getLogger("agents.sales")

SALES_HANDLE_ERRORS = SALES_EXTERNAL_ERRORS + (OSError,)
if agencies_asyncpg is not None:
    SALES_HANDLE_ERRORS = SALES_HANDLE_ERRORS + (agencies_asyncpg.PostgresError,)

class SalesAgent(SalesFlowHelpersMixin):
    """Agente comercial principal que enruta y mantiene la conversacion de ventas."""

    INFO_CHOICE_FULL_CATALOG_EXACT = frozenset({"todos", "todas", "ver todos", "todos por favor", "todas por favor"})

    def __init__(self, *, llm: OpenAIClient | None = None) -> None:
        """Inicializa el salesagent con la configuracion necesaria."""
        self.settings = get_settings()
        self.llm = llm or OpenAIClient()
        self.turn_interpreter = ActiveFlowTurnInterpreter(llm=self.llm, settings=self.settings)
        self.promotions = PromotionsAPI()
        self.agencies = AgenciesRepo()
        self.handoff = OdooChatClient()
        self.crm = OdooCRMClient()
        self.geocoder = GeocoderClient()

    def _detect_intent(self, text: str) -> str:
        """Detecta intent."""
        return detect_sales_intent(text)

    @staticmethod
    def _should_start_commercial_recommendation(analysis) -> bool:
        """Indica si recommendation start commercial se cumple."""
        return bool(
            analysis.has_explicit_catalog_terms
            and not analysis.wants_capture
            and not analysis.wants_full_catalog
        )

    @staticmethod
    def _choice_reply_has_recommendation_context(profile_updates: dict) -> bool:
        """Devuelve el contexto choice reply has recommendation."""
        return any(key in profile_updates for key in ("people", "devices", "space_size", "usage"))

    def _choice_reply_wants_full_catalog(self, analysis, profile_updates: dict) -> bool:
        """Devuelve el catalog choice reply wants full."""
        if analysis.wants_full_catalog or analysis.normalized in self.INFO_CHOICE_FULL_CATALOG_EXACT:
            return True
        return bool(
            analysis.has_explicit_catalog_terms
            and not analysis.wants_personalized_recommendation
            and not self._choice_reply_has_recommendation_context(profile_updates)
        )

    def _choice_reply_wants_recommendation(self, analysis, profile_updates: dict) -> bool:
        """Devuelve el recommendation choice reply wants."""
        if analysis.wants_personalized_recommendation:
            return True
        if self._choice_reply_has_recommendation_context(profile_updates):
            return True
        if profile_updates.get("segment") and not analysis.has_explicit_catalog_terms:
            return True
        return analysis.is_commercial_followup

    @staticmethod
    def _message_prefers_catalog_refresh(analysis, profile_updates: dict) -> bool:
        """Indica si el mensaje pide volver a mostrar catálogo/promociones en vez de seguir la entrevista."""
        normalized = str(analysis.normalized or "")
        asks_for_promotions = any(term in normalized for term in ("promo", "promos", "promocion", "promociones"))
        return bool(
            (analysis.wants_full_catalog or asks_for_promotions)
            and not analysis.wants_personalized_recommendation
            and not analysis.wants_capture
            and not any(key in profile_updates for key in ("people", "devices", "space_size", "usage"))
        )

    @staticmethod
    def _recommendation_profile_updates(analysis, user_message: str) -> dict:
        """Devuelve los updates recommendation perfil."""
        profile_updates = extract_recommendation_slot_updates(user_message)
        if analysis.catalog_segment and not profile_updates.get("segment"):
            profile_updates["segment"] = analysis.catalog_segment
        return profile_updates

    @staticmethod
    def _discovery_recommendation_prefix(profile_updates: dict) -> str:
        """Devuelve el prefix discovery recommendation."""
        return "Con gusto le ayudo."

    @staticmethod
    def _no_data_recommendation_prefix(profile_updates: dict) -> str:
        """Devuelve el prefix no data recommendation."""
        return "Para orientarle, no necesito sus datos."

    async def _continue_without_registration(self, *, sales_state: dict, user_message: str, analysis) -> AgentResult:
        """Devuelve el registration continue without."""
        sales_state["pending_intent"] = "commercial"
        sales_state["commercial_registration_declined"] = True
        profile_updates = self._recommendation_profile_updates(analysis, user_message)

        if self._choice_reply_wants_full_catalog(analysis, profile_updates):
            sales_state["commercial_catalog_requested"] = True
            sales_state["commercial_catalog_segment"] = (
                profile_updates.get("segment")
                or analysis.catalog_segment
                or sales_state.get("commercial_catalog_segment")
            )
            return await self._handle_commercial_catalog_without_registration(
                sales_state=sales_state,
                user_message=user_message,
                prefix_message="Le comparto todas las opciones que tengo ahora.",
            )

        return await self._start_commercial_recommendation(
            sales_state=sales_state,
            user_message=user_message,
            profile_updates=profile_updates or None,
            prefix_message=self._no_data_recommendation_prefix(profile_updates),
        )

    async def _handle_internal(self, message: InboundMessage, state: SessionState) -> AgentResult:
        """Maneja internal y avanza el flujo."""
        analysis = analyze_sales_message(message.mensaje)
        text = " ".join((message.mensaje or "").strip().split())
        sales_state = self._get_sales_state(state)
        commercial_session_active = (
            state.current_intent == "commercial"
            or sales_state.get("pending_intent") == "commercial"
            or sales_state.get("last_intent") == "commercial"
            or bool(sales_state.get("catalog_context"))
        )
        self._ensure_contact_defaults(sales_state, recipient=message.recipient)

        location_payload = message.location.model_dump(exclude_none=True) if message.location else None
        if await self._ingest_location_payload(sales_state, location_payload):
            logger.info("sales_location_ingested session_id=%s lead=%s", message.session_id, sales_state.get("lead"))

        intent = analysis.intent
        if not sales_state.get("greeted") and intent == "generic" and analysis.is_generic_opening:
            sales_state["pending_intent"] = None
            return self._respond(
                sales_state=sales_state,
                user_message=message.mensaje,
                message="",
                intent="welcome",
            )

        if intent in {"payments", "payment_methods", "pago_mensual"}:
            sales_state["pending_intent"] = None
            sales_state["awaiting_agency_location"] = False
            sales_state["awaiting_crm_field"] = None
            sales_state["commercial_catalog_requested"] = False
            return self._handle_general_intent(sales_state=sales_state, user_message=message.mensaje, intent=intent)

        if sales_state.get("awaiting_agency_location"):
            await self._ingest_text_location(sales_state, text)
            if not self._has_general_location(sales_state):
                return self._respond(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    message=AGENCY_PROMPT,
                    intent="agencies",
                )
            return await self._handle_agencies_lookup(sales_state=sales_state, user_message=message.mensaje)

        commercial_refresh_profile_updates = self._recommendation_profile_updates(analysis, message.mensaje)
        if commercial_session_active and self._message_prefers_catalog_refresh(analysis, commercial_refresh_profile_updates):
            sales_state["pending_intent"] = "commercial"
            sales_state["commercial_catalog_requested"] = True
            sales_state["commercial_catalog_segment"] = (
                analysis.catalog_segment
                or commercial_refresh_profile_updates.get("segment")
                or sales_state.get("commercial_catalog_segment")
                or (sales_state.get("recommendation_profile") or {}).get("segment")
            )
            return await self._handle_commercial_catalog_without_registration(
                sales_state=sales_state,
                user_message=message.mensaje,
                prefix_message="Le comparto las promociones y planes disponibles en este momento.",
            )

        if sales_state.get("awaiting_info_choice"):
            turn_decision = await self._interpret_active_commercial_turn(
                sales_state=sales_state,
                user_message=message.mensaje,
                analysis=analysis,
            )
            if turn_decision.action == "switch_intent":
                override = await self._handle_crm_capture_override(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    text=text,
                    intent=turn_decision.target_intent or intent,
                )
                if override is not None:
                    return override
            if turn_decision.action == "start_capture":
                sales_state["pending_intent"] = "commercial"
                sales_state["commercial_catalog_requested"] = True
                sales_state["commercial_registration_declined"] = False
                return await self._continue_crm_capture(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    channel=message.channel,
                    recipient=message.recipient,
                    cedula=message.cedula,
                    prefix_message="Perfecto. Para dejarlo listo sí necesito unos datos básicos.",
                )
            profile_updates = extract_recommendation_slot_updates(message.mensaje)
            if analysis.catalog_segment and not profile_updates.get("segment"):
                profile_updates["segment"] = analysis.catalog_segment
            if turn_decision.action == "show_catalog":
                sales_state["commercial_catalog_requested"] = True
                sales_state["commercial_catalog_segment"] = (
                    profile_updates.get("segment")
                    or analysis.catalog_segment
                    or sales_state.get("commercial_catalog_segment")
                )
                return await self._handle_commercial_catalog_without_registration(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    prefix_message="Le comparto todas las opciones que tengo ahora.",
                )
            if turn_decision.action == "start_recommendation":
                return await self._start_commercial_recommendation(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    profile_updates=profile_updates or None,
                    prefix_message="Perfecto.",
                )
            return await self._continue_without_registration(
                sales_state=sales_state,
                user_message=message.mensaje,
                analysis=analysis,
            )

        if sales_state.get("awaiting_recommendation_field"):
            turn_decision = await self._interpret_active_commercial_turn(
                sales_state=sales_state,
                user_message=message.mensaje,
                analysis=analysis,
            )
            profile_updates = self._recommendation_profile_updates(analysis, message.mensaje)
            if turn_decision.action == "switch_intent":
                override = await self._handle_crm_capture_override(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    text=text,
                    intent=turn_decision.target_intent or intent,
                )
                if override is not None:
                    return override
            if turn_decision.action == "start_capture":
                segment = (sales_state.get("recommendation_profile") or {}).get("segment") or sales_state.get(
                    "commercial_catalog_segment"
                )
                self._reset_commercial_recommendation(sales_state)
                sales_state["pending_intent"] = "commercial"
                sales_state["commercial_catalog_requested"] = True
                sales_state["commercial_catalog_segment"] = segment
                sales_state["commercial_registration_declined"] = False
                return await self._continue_crm_capture(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    channel=message.channel,
                    recipient=message.recipient,
                    cedula=message.cedula,
                    prefix_message="Perfecto. Para dejarlo listo sí necesito unos datos básicos.",
                )
            if self._message_prefers_catalog_refresh(analysis, profile_updates):
                sales_state["pending_intent"] = "commercial"
                sales_state["commercial_catalog_requested"] = True
                sales_state["commercial_catalog_segment"] = (
                    analysis.catalog_segment
                    or (sales_state.get("recommendation_profile") or {}).get("segment")
                    or sales_state.get("commercial_catalog_segment")
                )
                sales_state["commercial_registration_declined"] = True
                return await self._handle_commercial_catalog_without_registration(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    prefix_message="Le comparto las promociones y planes disponibles en este momento.",
                )
            if analysis.declines_registration:
                current_field = str(sales_state.get("awaiting_recommendation_field") or "").strip()
                prompt = recommendation_question(current_field, sales_state.get("recommendation_profile") or {})
                reassurance = "Para orientarle, no necesito sus datos."
                message_text = f"{reassurance}\n\n{prompt}" if prompt else reassurance
                response_plan = self._sales_response_plan(
                    sales_state=sales_state,
                    message=message_text,
                    conversation_state="recommendation_question",
                    reply_goal="reasegurar que la recomendación no requiere datos y mantener el hilo comercial",
                    next_step=f"capturar_{current_field}" if current_field else "seguir_recomendacion",
                    followup_prompt=prompt or reassurance,
                    hypothesis="collect_recommendation_profile",
                )
                return self._respond(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    message=message_text,
                    intent="commercial",
                    metadata=self._recommendation_metadata(sales_state),
                    response_plan=response_plan,
                )
            return await self._continue_commercial_recommendation(
                sales_state=sales_state,
                user_message=message.mensaje,
                text=text,
                wants_full_catalog=turn_decision.action == "show_catalog",
            )

        if sales_state.get("awaiting_crm_field"):
            field = sales_state["awaiting_crm_field"]
            turn_decision = await self._interpret_active_commercial_turn(
                sales_state=sales_state,
                user_message=message.mensaje,
                analysis=analysis,
            )
            if turn_decision.action == "switch_intent":
                override = await self._handle_crm_capture_override(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    text=text,
                    intent=turn_decision.target_intent or intent,
                )
                if override is not None:
                    return override
            if turn_decision.action == "decline_registration":
                return await self._continue_without_registration(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    analysis=analysis,
                )
            if turn_decision.action == "show_catalog":
                sales_state["commercial_catalog_requested"] = True
                sales_state["commercial_catalog_segment"] = (
                    analysis.catalog_segment or sales_state.get("commercial_catalog_segment")
                )
                sales_state["commercial_registration_declined"] = True
                return await self._handle_commercial_catalog_without_registration(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                )
            if field == "coordinates":
                await self._ingest_text_location(sales_state, text, allow_general_location=False)
                if not self._has_precise_location(sales_state):
                    return self._respond(
                        sales_state=sales_state,
                        user_message=message.mensaje,
                        message=CRM_QUESTIONS[field],
                        intent="commercial",
                    )
                return await self._continue_crm_capture(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    channel=message.channel,
                    recipient=message.recipient,
                    cedula=message.cedula,
                )

            if field == "city":
                city_status = await self._save_supported_city_answer(sales_state, text)
                if city_status == self.CITY_REPLY_SAVED:
                    return await self._continue_crm_capture(
                        sales_state=sales_state,
                        user_message=message.mensaje,
                        channel=message.channel,
                        recipient=message.recipient,
                        cedula=message.cedula,
                    )
                if await self._save_supported_city_from_location(sales_state):
                    return await self._continue_crm_capture(
                        sales_state=sales_state,
                        user_message=message.mensaje,
                        channel=message.channel,
                        recipient=message.recipient,
                        cedula=message.cedula,
                    )
                if city_status == self.CITY_REPLY_STREETLIKE:
                    response_plan = self._sales_response_plan(
                        sales_state=sales_state,
                        message=(
                            "Eso me suena más a dirección que a ciudad. "
                            "Primero indíqueme la ciudad donde sería la instalación y luego seguimos con la dirección exacta."
                        ),
                        conversation_state="crm_city_needs_city_not_street",
                        reply_goal="corregir el dato sin sonar rígido y mantener el avance comercial",
                        next_step="request_supported_city",
                        hypothesis="street_shared_when_city_was_requested",
                    )
                    return self._respond(
                        sales_state=sales_state,
                        user_message=message.mensaje,
                        message=response_plan.message,
                        intent="commercial",
                        response_plan=response_plan,
                    )
                if city_status == self.CITY_REPLY_UNSUPPORTED:
                    response_plan = self._sales_response_plan(
                        sales_state=sales_state,
                        message=(
                            "Por ahora no veo cobertura en esa ciudad. "
                            "Si lo prefiere, indíqueme otra ciudad donde le gustaría instalarlo y lo reviso."
                        ),
                        conversation_state="crm_city_unsupported",
                        reply_goal="informar cobertura sin cerrar la conversación",
                        next_step="request_supported_city",
                        hypothesis="unsupported_city_for_installation",
                    )
                    return self._respond(
                        sales_state=sales_state,
                        user_message=message.mensaje,
                        message=response_plan.message,
                        intent="commercial",
                        response_plan=response_plan,
                    )
                response_plan = self._sales_response_plan(
                    sales_state=sales_state,
                    message=CRM_QUESTIONS[field],
                    conversation_state="crm_city_retry",
                    reply_goal="retomar la captura de ciudad con una pregunta clara y natural",
                    next_step="request_supported_city",
                    hypothesis="city_not_understood_yet",
                )
                return self._respond(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    message=response_plan.message,
                    intent="commercial",
                    response_plan=response_plan,
                )
            if field == "street":
                await self._ingest_text_location(sales_state, text, allow_general_location=False)

            if not self._save_crm_answer(sales_state, field, text):
                if self._save_crm_answer_from_location(sales_state, field):
                    return await self._continue_crm_capture(
                        sales_state=sales_state,
                        user_message=message.mensaje,
                        channel=message.channel,
                        recipient=message.recipient,
                        cedula=message.cedula,
                    )
                response_plan = self._sales_response_plan(
                    sales_state=sales_state,
                    message=CRM_QUESTIONS[field],
                    conversation_state=f"crm_{field}_retry",
                    reply_goal="pedir de nuevo el dato faltante sin sonar a formulario",
                    next_step=f"request_{field}",
                    hypothesis=f"{field}_not_captured_yet",
                )
                return self._respond(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    message=response_plan.message,
                    intent="commercial",
                    response_plan=response_plan,
                )

            prefix = self._location_ack_prefix(sales_state)
            return await self._continue_crm_capture(
                sales_state=sales_state,
                user_message=message.mensaje,
                channel=message.channel,
                recipient=message.recipient,
                cedula=message.cedula,
                prefix_message=prefix or None,
            )

        if self._should_start_commercial_recommendation(analysis) and not (
            commercial_session_active and sales_state.get("recommended_plan")
        ):
            profile_updates = self._recommendation_profile_updates(analysis, message.mensaje)
            prefix_message = (
                COMMERCIAL_RECOMMENDATION_INFO_ONLY_MSG
                if analysis.declines_registration
                else None
            )
            return await self._start_commercial_recommendation(
                sales_state=sales_state,
                user_message=message.mensaje,
                profile_updates=profile_updates,
                prefix_message=prefix_message,
            )

        if intent == "menu":
            return self._respond(
                sales_state=sales_state,
                user_message=message.mensaje,
                message=MENU,
                intent="menu",
            )

        if intent == "discovery":
            profile_updates = self._recommendation_profile_updates(analysis, message.mensaje)
            return await self._start_commercial_recommendation(
                sales_state=sales_state,
                user_message=message.mensaje,
                profile_updates=profile_updates or None,
                prefix_message=self._discovery_recommendation_prefix(profile_updates),
            )

        if intent == "agencies":
            sales_state["pending_intent"] = "agencies"
            return await self._continue_agencies_followup(
                sales_state=sales_state,
                user_message=message.mensaje,
                text=text,
            )

        if state.current_intent == "agencies" and intent == "generic":
            return await self._continue_agencies_followup(
                sales_state=sales_state,
                user_message=message.mensaje,
                text=text,
            )

        catalog_plan = None
        if commercial_session_active and sales_state.get("catalog_context"):
            catalog_plan = self._find_catalog_plan_by_reference(sales_state, message.mensaje)

        if commercial_session_active and catalog_plan:
            sales_state["pending_intent"] = "commercial"
            sales_state["commercial_catalog_segment"] = (
                catalog_plan.get("segment") or sales_state.get("commercial_catalog_segment")
            )
            if analysis.accepts_recommended_plan or analysis.wants_capture:
                self._set_recommended_plan_from_catalog(sales_state, catalog_plan)
                sales_state["commercial_catalog_requested"] = True
                sales_state["commercial_registration_declined"] = False
                return await self._continue_crm_capture(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    channel=message.channel,
                    recipient=message.recipient,
                    cedula=message.cedula,
                    prefix_message=self._recommended_plan_capture_prefix(sales_state),
                )
            return self._respond(
                sales_state=sales_state,
                user_message=message.mensaje,
                message=self._build_catalog_plan_selection_message(
                    sales_state=sales_state,
                    plan=catalog_plan,
                    user_message=message.mensaje,
                ),
                intent="commercial",
                metadata=self._recommendation_metadata(sales_state),
            )

        recommended_plan_turn = None
        if commercial_session_active and sales_state.get("recommended_plan"):
            recommended_plan_turn = await self._interpret_active_commercial_turn(
                sales_state=sales_state,
                user_message=message.mensaje,
                analysis=analysis,
            )

        if (
            commercial_session_active
            and sales_state.get("recommended_plan")
            and recommended_plan_turn
            and recommended_plan_turn.action == "switch_intent"
        ):
            override = await self._handle_crm_capture_override(
                sales_state=sales_state,
                user_message=message.mensaje,
                text=text,
                intent=recommended_plan_turn.target_intent or intent,
            )
            if override is not None:
                return override

        if (
            commercial_session_active
            and sales_state.get("recommended_plan")
            and recommended_plan_turn
            and recommended_plan_turn.action == "accept_recommended_plan"
        ):
            sales_state["pending_intent"] = "commercial"
            sales_state["commercial_catalog_requested"] = True
            sales_state["commercial_catalog_segment"] = (
                (sales_state.get("recommendation_profile") or {}).get("segment")
                or (sales_state.get("recommended_plan") or {}).get("segment")
                or sales_state.get("commercial_catalog_segment")
            )
            sales_state["commercial_registration_declined"] = False
            return await self._continue_crm_capture(
                sales_state=sales_state,
                user_message=message.mensaje,
                channel=message.channel,
                recipient=message.recipient,
                cedula=message.cedula,
                prefix_message=self._recommended_plan_capture_prefix(sales_state),
            )

        if commercial_session_active and analysis.wants_capture:
            sales_state["pending_intent"] = "commercial"
            sales_state["commercial_catalog_requested"] = True
            sales_state["commercial_registration_declined"] = False
            return await self._continue_crm_capture(
                sales_state=sales_state,
                user_message=message.mensaje,
                channel=message.channel,
                recipient=message.recipient,
                cedula=message.cedula,
                prefix_message=(
                    self._recommended_plan_capture_prefix(sales_state)
                    if sales_state.get("recommended_plan")
                    else "Perfecto. Para dejarlo listo sí necesito unos datos básicos."
                ),
            )

        if (
            commercial_session_active
            and sales_state.get("recommended_plan")
            and recommended_plan_turn
            and recommended_plan_turn.action == "show_catalog"
        ):
            sales_state["pending_intent"] = "commercial"
            sales_state["commercial_catalog_requested"] = True
            if (sales_state.get("recommendation_profile") or {}).get("segment"):
                sales_state["commercial_catalog_segment"] = (sales_state.get("recommendation_profile") or {}).get("segment")
            if self._wants_higher_speed_options(analysis.normalized):
                return await self._handle_higher_speed_catalog_without_registration(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                )
            return await self._handle_commercial_catalog_without_registration(
                sales_state=sales_state,
                user_message=message.mensaje,
                prefix_message="Le comparto las otras opciones para que compare.",
            )

        if (
            commercial_session_active
            and sales_state.get("recommended_plan")
            and recommended_plan_turn
            and recommended_plan_turn.action == "unclear"
        ):
            return self._respond(
                sales_state=sales_state,
                user_message=message.mensaje,
                message=build_recommended_plan_followup_prompt(
                    sales_state.get("recommended_plan") or {},
                    sales_state.get("recommendation_profile") or {},
                ),
                intent="commercial",
                metadata=self._recommendation_metadata(sales_state),
            )

        if commercial_session_active and analysis.wants_full_catalog:
            sales_state["pending_intent"] = "commercial"
            sales_state["commercial_catalog_requested"] = True
            if (sales_state.get("recommendation_profile") or {}).get("segment"):
                sales_state["commercial_catalog_segment"] = (sales_state.get("recommendation_profile") or {}).get("segment")
            return await self._handle_commercial_catalog_without_registration(
                sales_state=sales_state,
                user_message=message.mensaje,
                prefix_message="Le comparto todas las opciones que tengo ahora.",
            )

        if commercial_session_active and intent == "generic" and analysis.is_commercial_followup:
            sales_state["pending_intent"] = "commercial"
            sales_state["commercial_catalog_requested"] = True
            sales_state["commercial_catalog_segment"] = (
                analysis.catalog_segment or sales_state.get("commercial_catalog_segment")
            )
            if sales_state.get("commercial_registration_declined"):
                return await self._continue_without_registration(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    analysis=analysis,
                )
            if sales_state.get("crm_lead_created"):
                return await self._handle_commercial_catalog(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    channel=message.channel,
                    recipient=message.recipient,
                    cedula=message.cedula,
                )

        if intent in {"plan", "info", "instalacion_tiempo", "instalacion_costo", "requisitos", "contrato", "garantia"}:
            sales_state["pending_intent"] = "commercial"
            sales_state["commercial_catalog_requested"] = True
            sales_state["commercial_catalog_segment"] = analysis.catalog_segment
            profile_updates = self._recommendation_profile_updates(analysis, message.mensaje)
            if self._message_prefers_catalog_refresh(analysis, profile_updates):
                sales_state["commercial_catalog_requested"] = True
                sales_state["commercial_catalog_segment"] = (
                    analysis.catalog_segment
                    or sales_state.get("commercial_catalog_segment")
                )
                return await self._handle_commercial_catalog_without_registration(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    prefix_message="Le comparto las promociones y planes disponibles en este momento.",
                )
            if sales_state.get("commercial_registration_declined") or analysis.declines_registration:
                return await self._continue_without_registration(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    analysis=analysis,
                )
            if not analysis.wants_capture and not analysis.wants_full_catalog and (
                analysis.is_commercial_followup or bool(profile_updates)
            ):
                return await self._start_commercial_recommendation(
                    sales_state=sales_state,
                    user_message=message.mensaje,
                    profile_updates=profile_updates or None,
                    prefix_message=self._discovery_recommendation_prefix(profile_updates),
                )
            prefix = self._location_ack_prefix(sales_state)
            message_prefix = INFO_INTRO if not prefix else f"{prefix}{INFO_INTRO}"
            return await self._continue_crm_capture(
                sales_state=sales_state,
                user_message=message.mensaje,
                channel=message.channel,
                recipient=message.recipient,
                cedula=message.cedula,
                prefix_message=message_prefix,
            )

        if intent == "contacto":
            return self._handle_general_intent(sales_state=sales_state, user_message=message.mensaje, intent=intent)

        if intent == "greeting":
            sales_state["pending_intent"] = None
            return self._respond(
                sales_state=sales_state,
                user_message=message.mensaje,
                message="",
                intent="welcome",
            )

        sales_state["pending_intent"] = None
        return self._respond(
            sales_state=sales_state,
            user_message=message.mensaje,
            message=OUT_OF_SCOPE_MSG,
            intent="generic",
            metadata={"lead": sales_state.get("lead") or {}},
        )

    async def handle(self, message: InboundMessage, state: SessionState) -> AgentResult:
        """Maneja la entrada completa y devuelve el resultado final."""
        try:
            logger.info(
                "sales_handle_start session_id=%s recipient=%s pending_intent=%s awaiting_crm_field=%s awaiting_agency_location=%s preview=%r",
                message.session_id,
                message.recipient,
                ((state.metadata.get("sales") or {}).get("pending_intent")),
                ((state.metadata.get("sales") or {}).get("awaiting_crm_field")),
                ((state.metadata.get("sales") or {}).get("awaiting_agency_location")),
                " ".join((message.mensaje or "").split())[:160],
            )
            result = await self._handle_internal(message, state)
            if str(result.message or "").strip().lower().startswith(("hola", "buenos", "buenas")):
                state.metadata["assistant_intro_sent"] = True
            logger.info(
                "sales_handle_done session_id=%s intent=%s preview=%r",
                message.session_id,
                result.intent,
                " ".join((result.message or "").split())[:160],
            )
            return result
        except SALES_HANDLE_ERRORS as exc:
            logger.exception("sales_internal_flow_failed session_id=%s", message.session_id)
            sales_state = self._get_sales_state(state)
            summary = (
                "Escalamiento automático por falla operativa en ventas. "
                f"session_id={message.session_id}. "
                f"recipient={message.recipient}. "
                f"mensaje_usuario={(message.mensaje or '').strip()[:240]!r}. "
                f"error={exc.__class__.__name__}: {exc}"
            )
            if sales_state.get("recommended_plan"):
                summary += f" Plan recomendado={(sales_state.get('recommended_plan') or {}).get('name')}."
            return await create_direct_handoff_result(
                odoo=self.handoff,
                message=message,
                state=state,
                summary=summary,
                group="iainfo",
                origen=self.settings.info_origen,
                final_message=(
                    "Tuve un inconveniente mientras revisaba su solicitud comercial y voy a dejarla "
                    "con un asesor especializado comercial para continuar con la atención."
                ),
                failure_message=(
                    "Tuve un inconveniente mientras revisaba su solicitud comercial y tampoco pude completar "
                    "la derivación automática con un asesor especializado. Por favor, vuelva a escribir en unos minutos."
                ),
                hypothesis="internal_sales_flow_error",
                reply_goal="confirmar con claridad que la solicitud comercial ya quedó derivada con un asesor especializado",
                error_type=exc.__class__.__name__,
            )
