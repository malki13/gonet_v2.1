"""Acceso al catálogo de agencias y sus datos operativos."""

import logging

from packages.shared.config import get_settings

logger = logging.getLogger("agencies_repo")

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None


class AgenciesRepo:
    """Repositorio de agencias y sus datos de apoyo.."""
    def __init__(self) -> None:
        """Inicializa el agenciesrepo con la configuracion necesaria."""
        self.settings = get_settings()

    def _dsn(self) -> str | None:
        """Devuelve el dsn."""
        return self.settings.agencies_pg_dsn or self.settings.pg_dsn

    @staticmethod
    def _mock_rows() -> list[dict]:
        """Devuelve los filas mock."""
        return [
            {
                "agencia": "Agencia Centro",
                "ciudad": "Guayaquil",
                "provincia": "Guayas",
                "direccion": "Av. Demo 123 y Primera",
                "horarios": "Lun a Vie 08:00 - 17:00",
                "telefono": "04-600-0001",
            },
            {
                "agencia": "Agencia Norte",
                "ciudad": "Quito",
                "provincia": "Pichincha",
                "direccion": "Av. Demo 456 y Segunda",
                "horarios": "Lun a Vie 08:30 - 17:30",
                "telefono": "02-600-0002",
            },
        ]

    async def by_city(self, city_upper: str) -> list[dict]:
        """Devuelve el city by."""
        dsn = self._dsn()
        if not dsn or asyncpg is None:
            if self.settings.mock_mode:
                needle = (city_upper or "").strip().lower()
                return [row for row in self._mock_rows() if needle in (row.get("ciudad") or "").lower()]
            return []
        query = "SELECT * FROM agencias WHERE ciudad ILIKE $1"
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch(query, f"%{city_upper}%")
            return [dict(row) for row in rows]
        finally:
            await conn.close()

    async def by_province(self, province_upper: str) -> list[dict]:
        """Devuelve el province by."""
        dsn = self._dsn()
        if not dsn or asyncpg is None:
            if self.settings.mock_mode:
                needle = (province_upper or "").strip().lower()
                return [row for row in self._mock_rows() if needle in (row.get("provincia") or "").lower()]
            return []
        query = "SELECT * FROM agencias WHERE provincia ILIKE $1"
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch(query, f"%{province_upper}%")
            return [dict(row) for row in rows]
        finally:
            await conn.close()
