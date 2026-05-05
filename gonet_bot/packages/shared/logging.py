"""Configuracion estandar de logging para toda la repo."""

import logging

from packages.shared.config import get_settings


def setup_logging() -> None:
    """Configura el logging central de la aplicación."""
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
