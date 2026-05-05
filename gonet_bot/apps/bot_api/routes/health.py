"""Rutas simples de verificacion de salud del servicio."""

from fastapi import APIRouter

from packages.shared.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Devuelve un `200 OK` minimo para comprobar que la API sigue viva."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.app_env,
    }
