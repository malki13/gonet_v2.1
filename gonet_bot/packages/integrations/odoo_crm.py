"""Cliente de CRM para crear y consultar leads en Odoo."""

import asyncio
import logging
from urllib.parse import urljoin
from xmlrpc.client import Fault, ProtocolError, ServerProxy

from packages.shared.config import get_settings

logger = logging.getLogger("integrations.odoo_crm")


class OdooCRMClient:
    """Cliente de Integracion con el CRM de Odoo.."""
    def __init__(self) -> None:
        """Inicializa el odoocrmclient con la configuracion necesaria."""
        self.settings = get_settings()

    def _is_configured(self) -> bool:
        """Indica si hay datos suficientes para hablar con Odoo por JSON-RPC."""
        return all(
            [
                self.settings.odoo_url,
                self.settings.odoo_db,
                self.settings.odoo_username,
                self.settings.odoo_password,
            ]
        )

    @staticmethod
    def _lead_name(payload: dict) -> str:
        """Devuelve el nombre lead."""
        partner_name = payload.get("partner_name")
        phone = payload.get("phone")
        street = payload.get("street")
        if partner_name:
            return f"Lead Bot - {partner_name}"
        if phone:
            return f"Lead Bot - {phone}"
        if street:
            return f"Lead Bot - {street}"
        return "Lead Bot"

    def _build_payload(self, payload: dict) -> dict:
        """Construye payload a partir del contexto disponible."""
        return {
            "name": self._lead_name(payload),
            "type": payload["type"],
            "partner_name": payload["partner_name"],
            "city": payload.get("city"),
            "street": payload["street"],
            "phone": payload["phone"],
            "latitude": payload.get("latitude"),
            "longitude": payload.get("longitude"),
        }

    def _common_url(self) -> str:
        """Devuelve el URL common."""
        return urljoin(self.settings.odoo_url.rstrip("/") + "/", "xmlrpc/2/common")

    def _object_url(self) -> str:
        """Devuelve el URL object."""
        return urljoin(self.settings.odoo_url.rstrip("/") + "/", "xmlrpc/2/object")

    def _create_sync(self, payload: dict) -> dict:
        """Devuelve el sync create."""
        common = ServerProxy(self._common_url(), allow_none=True)
        uid = common.authenticate(
            self.settings.odoo_db,
            self.settings.odoo_username,
            self.settings.odoo_password,
            {},
        )
        if not uid:
            raise RuntimeError("Odoo authentication failed")

        proxy = ServerProxy(self._object_url(), allow_none=True)
        odoo_payload = self._build_payload(payload)
        lead_id = proxy.execute_kw(
            self.settings.odoo_db,
            uid,
            self.settings.odoo_password,
            self.settings.odoo_lead_model,
            "create",
            [odoo_payload],
        )
        return {"status": "created", "payload": payload, "odoo_payload": odoo_payload, "response": {"id": lead_id}}

    async def create_lead(self, payload: dict) -> dict:
        """Devuelve el lead create."""
        required = ["type", "partner_name", "street", "phone", "latitude", "longitude"]
        missing = [field for field in required if not payload.get(field)]
        if missing:
            logger.warning("odoo_crm_invalid_payload missing=%s payload=%s", missing, payload)
            return {"status": "invalid_payload", "payload": payload, "missing": missing}
        if not self._is_configured():
            if self.settings.mock_mode:
                return {"status": "created", "payload": payload, "response": {"id": 70001}, "source": "mock"}
            return {"status": "skipped", "payload": payload}
        try:
            return await asyncio.to_thread(self._create_sync, payload)
        except (Fault, ProtocolError, OSError, RuntimeError) as exc:
            logger.exception("odoo_crm_create_failed model=%s payload=%s", self.settings.odoo_lead_model, payload)
            return {"status": "error", "payload": payload, "error": f"{exc.__class__.__name__}: {exc}"}
