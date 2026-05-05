"""Outbox de desarrollo para inspeccionar mensajes salientes."""

from fastapi import APIRouter, Query, Request

from apps.bot_api.security import enforce_internal_auth
from packages.integrations.outbox_store import build_outbox_store
from packages.shared.schemas import OutboundDelivery

router = APIRouter(prefix="/v1/outbound", tags=["outbound"])
store = build_outbox_store()


@router.post("")
async def create_outbound(request: Request, delivery: OutboundDelivery) -> dict:
    """Devuelve el outbound create."""
    enforce_internal_auth(request)
    await store.add(delivery)
    return {"status": "queued", "delivery": delivery.model_dump()}


@router.get("")
async def list_outbound(
    request: Request,
    session_id: str | None = Query(default=None),
    recipient: str | None = Query(default=None),
) -> dict:
    """Lista la API del gateway."""
    enforce_internal_auth(request)
    items = await store.list_messages(session_id=session_id, recipient=recipient)
    return {"status": "ok", "count": len(items), "items": [item.model_dump() for item in items]}
