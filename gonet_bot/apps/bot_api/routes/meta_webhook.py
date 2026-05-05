"""Webhook tipado para integraciones internas que reusan el contrato de Meta."""

from fastapi import APIRouter, Depends, Request

from apps.bot_api.dependencies import get_orchestrator
from apps.bot_api.security import enforce_internal_auth
from packages.channels.meta_inbound import normalize_meta_payload
from packages.orchestrator.service import OrchestratorService
from packages.shared.schemas import MetaWebhookPayload, OutboundMessage

router = APIRouter(prefix="/v1/webhooks", tags=["meta"])


@router.post("/meta", response_model=OutboundMessage)
async def handle_meta_webhook(
    request: Request,
    payload: MetaWebhookPayload,
    orchestrator: OrchestratorService = Depends(get_orchestrator),
) -> OutboundMessage:
    """Maneja meta webhook y avanza el flujo."""
    enforce_internal_auth(request)
    normalized = normalize_meta_payload(payload)
    return await orchestrator.handle_message(normalized)
