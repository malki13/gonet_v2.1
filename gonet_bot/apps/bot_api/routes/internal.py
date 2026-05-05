"""Rutas internas de entrada al orquestador."""

import logging

from fastapi import APIRouter, Depends, Request

from apps.bot_api.dependencies import get_orchestrator
from apps.bot_api.security import enforce_internal_auth
from packages.orchestrator.service import OrchestratorService
from packages.shared.schemas import InboundMessage, OutboundMessage

router = APIRouter(prefix="/v1", tags=["internal"])
logger = logging.getLogger("internal_api")


@router.post("/messages", response_model=OutboundMessage)
async def handle_message(
    request: Request,
    payload: InboundMessage,
    orchestrator: OrchestratorService = Depends(get_orchestrator),
) -> OutboundMessage:
    """Maneja mensaje y avanza el flujo."""
    enforce_internal_auth(request)
    logger.info(
        "internal_message_received session_id=%s channel=%s recipient=%s attachments=%s preview=%r",
        payload.session_id,
        payload.channel,
        payload.recipient,
        len(payload.attachments or []),
        " ".join((payload.mensaje or "").split())[:160],
    )
    response = await orchestrator.handle_message(payload)
    logger.info(
        "internal_message_response session_id=%s agent=%s intent=%s preview=%r",
        payload.session_id,
        response.agent,
        response.intent,
        " ".join((response.message or "").split())[:160],
    )
    return response
