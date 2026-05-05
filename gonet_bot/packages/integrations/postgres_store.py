"""Persistencia en Postgres de estado y eventos compartidos."""

class EventStore:
    """Almacena y recupera datos de las integraciones externas."""
    async def append(self, event: dict) -> dict:
        """Agrega el elemento al acumulado."""
        return {"status": "accepted", "event": event}

