"""Cliente para consultas de ONU y diagnostico de red."""

import logging
from typing import Any

import httpx

from packages.integrations.runtime import get_shared_http_client
from packages.shared.config import get_settings

logger = logging.getLogger("onu")


def _log_http_response(tag: str, response: httpx.Response) -> None:
    """Devuelve la respuesta log http."""
    snippet = (response.text or "").strip()[:800]
    logger.info("%s status=%s url=%s body=%s", tag, response.status_code, str(response.url), snippet)


class ONUClient:
    """Cliente de Integracion para administrar equipos ONU.."""
    def __init__(self) -> None:
        """Inicializa el onuclient con la configuracion necesaria."""
        self.settings = get_settings()

    def enabled(self) -> bool:
        """Indica si la integracion esta habilitada por configuracion."""
        if self.settings.mock_mode:
            return True
        return bool(self.settings.onu_base_url)

    def _base_url(self) -> str:
        """Devuelve el URL base."""
        return (self.settings.onu_base_url or "").rstrip("/")

    @staticmethod
    def _normalize_status(value: Any) -> str | None:
        """Normaliza status."""
        if value in (None, ""):
            return None
        lowered = str(value).strip().lower()
        aliases = {
            "up": "working",
            "ok": "working",
            "working": "working",
            "los": "los",
            "dyinggasp": "dyinggasp",
            "diyinggasp": "dyinggasp",
            "down": "down",
        }
        return aliases.get(lowered, lowered)

    @staticmethod
    def _parse_power(value: Any) -> float | None:
        """Devuelve el power parse."""
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _normalize_payload(cls, payload: Any) -> dict[str, Any]:
        """Normaliza payload."""
        raw_data = payload.get("data") if isinstance(payload, dict) else payload
        status = None
        power_dbm = None
        if isinstance(raw_data, list):
            if raw_data:
                status = cls._normalize_status(raw_data[0])
            if len(raw_data) > 1:
                power_dbm = cls._parse_power(raw_data[1])
        elif isinstance(raw_data, dict):
            status = cls._normalize_status(
                raw_data.get("status")
                or raw_data.get("estado")
                or raw_data.get("onu_status")
                or raw_data.get("alarm")
            )
            power_dbm = cls._parse_power(
                raw_data.get("power_dbm")
                or raw_data.get("power")
                or raw_data.get("potencia")
                or raw_data.get("rx_power")
                or raw_data.get("rxPower")
            )
        else:
            status = cls._normalize_status(payload)
        return {
            "ok": True,
            "status": status,
            "power_dbm": power_dbm,
            "raw": payload,
        }

    async def _request_status(self, *, contrato: str) -> dict[str, Any]:
        """Devuelve los status request."""
        client = await get_shared_http_client()
        response = await client.get(f"{self._base_url()}/onu", params={"contrato": contrato}, timeout=30)
        _log_http_response("onu_status_get", response)
        response.raise_for_status()
        return self._normalize_payload(response.json())

    async def get_status(self, contrato: str) -> dict[str, Any]:
        """Devuelve status."""
        if self.settings.mock_mode and not self.settings.onu_base_url:
            return {
                "ok": True,
                "status": "working",
                "power_dbm": -26.2,
                "raw": {"data": ["working", -26.2]},
                "source": "mock",
            }
        try:
            return await self._request_status(contrato=contrato)
        except Exception as exc:
            logger.warning("onu_status_request_failed contrato=%s error=%s", contrato, exc)
            raise RuntimeError(f"No se pudo consultar el estado ONU del contrato {contrato}") from exc

    async def reboot(self, contrato: str) -> dict[str, Any]:
        """Ordena el reinicio remoto del equipo."""
        if self.settings.mock_mode and not self.settings.onu_base_url:
            return {"ok": True, "status": "accepted", "contrato": contrato, "source": "mock"}
        client = await get_shared_http_client()
        response = await client.post(f"{self._base_url()}/reiniciar/onu", data={"contrato": contrato}, timeout=30)
        _log_http_response("onu_reboot", response)
        response.raise_for_status()
        try:
            payload = response.json()
        except Exception:
            payload = {"body": response.text}
        return {"ok": True, "status": "accepted", "contrato": contrato, "raw": payload}
