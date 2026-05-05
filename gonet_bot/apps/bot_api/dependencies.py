"""Dependencias compartidas para las rutas de `apps.bot_api`."""

from functools import lru_cache

from packages.orchestrator.service import OrchestratorService


@lru_cache(maxsize=1)
def get_orchestrator() -> OrchestratorService:
    """Devuelve una instancia cacheada del orquestador compartido."""
    return OrchestratorService()
