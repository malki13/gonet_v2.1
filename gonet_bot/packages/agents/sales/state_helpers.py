"""Lectura y actualización del estado comercial de la sesión."""

import logging
import re
from urllib.parse import urljoin

import httpx

from packages.agents.sales.constants import CRM_FIELD_ORDER, MAX_HISTORY_MESSAGES, SALES_EXTERNAL_ERRORS, WELCOME_MSG
from packages.agents.sales.utils import (
    _extract_coordinates_from_text,
    _extract_location_from_text,
    _extract_urls,
    _is_google_maps_short_link,
    _looks_like_precise_address,
    _street_candidate,
    _title_case,
)
from packages.shared.assistant_persona import assistant_intro_prefix, assistant_welcome_prompt, ensure_assistant_greeting_style
from packages.shared.response_planner import response_plan_metadata
from packages.shared.schemas import AgentResult, ResponsePlan, SessionState

logger = logging.getLogger("agents.sales")


class SalesStateHelpersMixin:
    """Agrupa las ayudas de la fase comercial: catálogo, recomendación y captura."""
    
    @staticmethod
    def _strip_redundant_opening(message: str) -> str:
        """Devuelve el opening strip redundant."""
        cleaned = str(message or "").strip()
        patterns = (
            r"^(?:si|sí),?\s+te\s+ayudo\.?\s*",
            r"^claro,?\s+te\s+ayudo\.?\s*",
            r"^claro(?:,|\.)?\s*",
        )
        for pattern in patterns:
            updated = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
            if updated != cleaned:
                cleaned = updated.strip()
                break
        return cleaned.lstrip(" ,;:-")

    @staticmethod
    def _normalize_sales_state(session_id: str, state: dict | None) -> dict:
        """Normaliza estado comercial."""
        state = state or {}
        state.setdefault("history", [])
        state.setdefault("lead", {})
        state.setdefault("agent_name", None)
        state.setdefault("assistant_profile", {})
        state.setdefault("pending_intent", None)
        state.setdefault("awaiting_agency_location", False)
        state.setdefault("awaiting_crm_field", None)
        state.setdefault("crm_lead_created", False)
        state.setdefault("commercial_catalog_requested", False)
        state.setdefault("commercial_catalog_segment", None)
        state.setdefault("commercial_registration_declined", False)
        state.setdefault("commercial_handoff_requested", False)
        state.setdefault("awaiting_info_choice", False)
        state.setdefault("awaiting_recommendation_field", None)
        state.setdefault("recommendation_profile", {})
        state.setdefault("recommended_plan", None)
        state.setdefault("catalog_context", None)
        state.setdefault("selected_catalog_plan", None)
        state.setdefault("last_actions", None)
        state.setdefault("greeted", False)
        state.setdefault("fresh_location", False)
        return state

    def _get_sales_state(self, state: SessionState) -> dict:
        """Devuelve el estado comercial."""
        sales_state = self._normalize_sales_state(state.session_id, state.metadata.get("sales"))
        assistant_profile = (state.metadata or {}).get("assistant_profile")
        if isinstance(assistant_profile, dict) and assistant_profile:
            sales_state["assistant_profile"] = assistant_profile
            assistant_name = str(assistant_profile.get("display_name") or "").strip()
            if assistant_name:
                sales_state["agent_name"] = assistant_name
        if (state.metadata or {}).get("assistant_intro_sent"):
            sales_state["greeted"] = True
        state.metadata["sales"] = sales_state
        return sales_state

    @staticmethod
    def _append_history(state: dict, *, role: str, content: str) -> None:
        """Agrega historial."""
        if not content:
            return
        history = state.setdefault("history", [])
        history.append({"role": role, "content": content})
        if len(history) > MAX_HISTORY_MESSAGES:
            del history[:-MAX_HISTORY_MESSAGES]

    def _with_greeting(self, state: dict, message: str) -> str:
        """Devuelve el greeting with."""
        cleaned = self._strip_redundant_opening(message)
        if state.get("greeted"):
            return cleaned or (message or "").strip()
        state["greeted"] = True
        agent_name = str(state.get("agent_name") or "").strip()
        assistant_profile = state.get("assistant_profile") if isinstance(state.get("assistant_profile"), dict) else {}
        ensure_assistant_greeting_style(assistant_profile)
        intro = assistant_intro_prefix(
            assistant_name=agent_name,
            assistant_profile=assistant_profile,
        )
        welcome = f"{intro}{assistant_welcome_prompt(assistant_profile)}"
        if not cleaned or cleaned == WELCOME_MSG:
            return welcome
        return f"{intro}{cleaned}"

    def _respond(
        self,
        *,
        sales_state: dict,
        user_message: str,
        message: str,
        intent: str,
        actions=None,
        metadata: dict | None = None,
        response_plan=None,
    ) -> AgentResult:
        """Devuelve el respond."""
        final_message = self._with_greeting(sales_state, message)
        self._append_history(sales_state, role="user", content=user_message)
        self._append_history(sales_state, role="assistant", content=final_message)
        sales_state["last_intent"] = intent
        sales_state["last_actions"] = actions
        metadata = dict(metadata or {})
        if isinstance(response_plan, ResponsePlan):
            response_plan = response_plan.model_copy(update={"message": final_message})
        metadata = response_plan_metadata(response_plan, metadata)
        assistant_profile = sales_state.get("assistant_profile")
        if isinstance(assistant_profile, dict) and assistant_profile:
            metadata["assistant_profile"] = assistant_profile
        return AgentResult(
            message=final_message,
            intent=intent,
            agent="sales",
            actions=actions,
            metadata=metadata,
        )

    @staticmethod
    def _has_general_location(state: dict) -> bool:
        """Indica si location general se cumple."""
        lead = state.get("lead") or {}
        return bool(lead.get("city") or lead.get("province") or lead.get("address"))

    @staticmethod
    def _has_precise_location(state: dict) -> bool:
        """Indica si location precise se cumple."""
        lead = state.get("lead") or {}
        return lead.get("latitude") is not None and lead.get("longitude") is not None

    @staticmethod
    def _looks_like_phone(value: str | None) -> bool:
        """Devuelve el phone looks like."""
        digits = re.sub(r"\D", "", value or "")
        return len(digits) >= 7

    def _ensure_contact_defaults(self, sales_state: dict, *, recipient: str) -> None:
        """Devuelve el defaults ensure contact."""
        lead = sales_state.setdefault("lead", {})
        if not lead.get("phone") and self._looks_like_phone(recipient):
            lead["phone"] = recipient

    def _next_crm_field(self, sales_state: dict) -> str | None:
        """Devuelve el field next crm."""
        if sales_state.get("crm_lead_created"):
            return None
        lead = sales_state.get("lead") or {}
        for field in CRM_FIELD_ORDER:
            if field == "phone":
                if self._looks_like_phone(lead.get("phone")):
                    continue
                return field
            if field == "coordinates":
                if self._has_precise_location(sales_state):
                    continue
                return field
            if not lead.get(field):
                return field
        return None

    def _location_label(self, sales_state: dict) -> str:
        """Devuelve la etiqueta location."""
        lead = sales_state.get("lead") or {}
        label = ", ".join(piece for piece in [lead.get("city"), lead.get("zone")] if piece)
        if label:
            return label
        return lead.get("province") or lead.get("address") or "la ubicación compartida"

    def _location_ack_prefix(self, sales_state: dict) -> str:
        """Devuelve el prefix location ack."""
        if not sales_state.pop("fresh_location", False):
            return ""
        return f"Listo, ya tengo {self._location_label(sales_state)}. "

    @staticmethod
    def _merge_location_data(sales_state: dict, data: dict) -> bool:
        """Fusiona location data."""
        if not data:
            return False
        lead = sales_state.setdefault("lead", {})
        changed = False
        for key in ("city", "province", "zone", "address"):
            value = _title_case(data.get(key))
            if value and lead.get(key) != value:
                lead[key] = value
                changed = True
        for key in ("latitude", "longitude"):
            value = data.get(key)
            if value is not None and lead.get(key) != value:
                lead[key] = value
                changed = True
        if changed:
            sales_state["fresh_location"] = True
        return changed

    async def _ingest_location_payload(self, sales_state: dict, location: dict | None) -> bool:
        """Devuelve el payload ingest location."""
        if not location:
            return False
        merged = dict(location)
        lat = merged.get("latitude")
        lon = merged.get("longitude")
        if lat is not None and lon is not None and not any(merged.get(key) for key in ("city", "province", "zone", "address")):
            geocoded = await self.geocoder.reverse(latitude=lat, longitude=lon)
            merged = {**geocoded, **merged}
        return self._merge_location_data(sales_state, merged)

    async def _resolve_google_maps_short_link(self, url: str) -> str:
        """Resuelve link google maps short."""
        current = url
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                headers={"User-Agent": self.settings.geocoder_user_agent},
                follow_redirects=False,
            ) as client:
                for _ in range(5):
                    if _extract_coordinates_from_text(current):
                        return current
                    response = await client.get(current)
                    if not response.is_redirect:
                        return str(response.url)
                    location = response.headers.get("location")
                    if not location:
                        return str(response.url)
                    current = urljoin(current, location)
        except SALES_EXTERNAL_ERRORS:
            logger.exception("google_maps_short_link_resolve_failed url=%s", url)
            return url
        return current

    async def _expand_google_maps_short_links(self, text: str) -> str:
        """Devuelve el links expand google maps short."""
        expanded = text or ""
        for raw_url in _extract_urls(text):
            url = raw_url.rstrip(").,;")
            if not _is_google_maps_short_link(url):
                continue
            resolved = await self._resolve_google_maps_short_link(url)
            if not resolved or resolved == url:
                continue
            suffix = raw_url[len(url) :]
            expanded = expanded.replace(raw_url, f"{resolved}{suffix}", 1)
        return expanded

    async def _ingest_text_location(
        self,
        sales_state: dict,
        text: str,
        *,
        allow_general_location: bool = True,
        allow_forward_geocoding: bool = True,
    ) -> bool:
        """Devuelve el location ingest texto."""
        merged = _extract_coordinates_from_text(text)
        if not merged:
            expanded = await self._expand_google_maps_short_links(text)
            if expanded != text:
                merged = _extract_coordinates_from_text(expanded)
        general_location = _extract_location_from_text(text) if allow_general_location else {}
        if allow_general_location:
            merged = {**general_location, **merged}
        if merged.get("latitude") is not None and merged.get("longitude") is not None:
            geocoded = await self.geocoder.reverse(latitude=merged["latitude"], longitude=merged["longitude"])
            merged = {**geocoded, **merged}
        elif allow_forward_geocoding and _looks_like_precise_address(text):
            lead = sales_state.setdefault("lead", {})
            query = _street_candidate(text) or general_location.get("address") or text
            try:
                geocoded = await self.geocoder.forward(
                    query,
                    city=lead.get("city") or general_location.get("city"),
                    province=lead.get("province") or general_location.get("province"),
                )
            except SALES_EXTERNAL_ERRORS:
                logger.exception("forward_geocode_failed query=%r", query)
                geocoded = {}
            if geocoded:
                merged = {**merged, **geocoded}
        return self._merge_location_data(sales_state, merged)

    @staticmethod
    def _reset_agency_location_scope(sales_state: dict) -> None:
        """Reinicia agency location scope para comenzar de nuevo."""
        lead = sales_state.setdefault("lead", {})
        for key in ("city", "province", "zone", "address", "street", "latitude", "longitude"):
            lead.pop(key, None)
        sales_state["fresh_location"] = False
