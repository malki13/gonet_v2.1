"""Funciones auxiliares para avanzar el flujo comercial."""

from packages.agents.sales.commercial_helpers import SalesCommercialHelpersMixin
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
    DISCOVERY_MSG,
    INFO_INTRO,
    LOCATION_FALLBACK_PROMPT,
    LOCATION_PROMPT,
    MAIN_MENU_ACTIONS,
    MENU,
    OUT_OF_SCOPE_MSG,
    PAYMENTS_REDIRECT_MSG,
    SALES_EXTERNAL_ERRORS,
    WELCOME_MSG,
)
from packages.agents.sales.state_helpers import SalesStateHelpersMixin


class SalesFlowHelpersMixin(SalesCommercialHelpersMixin, SalesStateHelpersMixin):
    """Agrupa las ayudas de la fase comercial: catálogo, recomendación y captura."""
    pass


__all__ = [
    "AGENCY_PROMPT",
    "COMMERCIAL_FOLLOWUP_MSG",
    "COMMERCIAL_INFO_CHOICE_MSG",
    "COMMERCIAL_INFO_ONLY_MSG",
    "COMMERCIAL_RECOMMENDATION_INFO_ONLY_MSG",
    "COMMERCIAL_RECOMMENDATION_MSG",
    "CRM_CREATED_MSG",
    "CRM_PENDING_MSG",
    "CRM_QUESTIONS",
    "DISCOVERY_MSG",
    "INFO_INTRO",
    "LOCATION_FALLBACK_PROMPT",
    "LOCATION_PROMPT",
    "MAIN_MENU_ACTIONS",
    "MENU",
    "OUT_OF_SCOPE_MSG",
    "PAYMENTS_REDIRECT_MSG",
    "SALES_EXTERNAL_ERRORS",
    "SalesFlowHelpersMixin",
    "WELCOME_MSG",
]
