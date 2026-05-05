"""Cliente para consultar contratos asociados a una cédula."""

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from packages.integrations.odoo_jsonrpc import OdooJsonRpcClient

logger = logging.getLogger("contract_lookup")
CONTRACT_LOOKUP_RETRY_DELAYS = (0.75, 1.5)


def _normalize_contract_lookup_result(result: Any) -> Any:
    """Normaliza resultado contract lookup."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            return []
    return result


def _result_size(result: Any) -> int:
    """Devuelve el size resultado."""
    if isinstance(result, list):
        return len(result)
    return 1 if result else 0


def _digits(value: Any) -> str:
    """Devuelve el digits."""
    return re.sub(r"\D", "", str(value or ""))


def _placeholder(value: Any) -> bool:
    """Devuelve el placeholder."""
    text = str(value or "").strip()
    if not text:
        return True
    normalized = text.lower()
    return normalized in {"s/n", "sn", "none", "null", "false", "0.0, 0.0"}


def _pick_text(*values: Any) -> str:
    """Devuelve el texto pick."""
    for value in values:
        text = str(value or "").strip()
        if not _placeholder(text):
            return text
    return ""


def _format_decimal(value: Any) -> str:
    """Da formato a decimal para presentarlo de forma clara."""
    text = str(value or "").strip()
    if not text:
        return "0.00"
    cleaned = text.replace(",", ".")
    try:
        return f"{float(cleaned):.2f}"
    except ValueError:
        return "0.00"


class ContractLookupClient:
    """Cliente que cruza Odoo y el buscador de contact center para recuperar contratos."""
    def __init__(self) -> None:
        """Inicializa el cliente que cruza odoo y el buscador de contact center para recuperar contratos con la configuracion necesaria."""
        self.client = OdooJsonRpcClient(
            logger=logger,
            request_log_tag="odoo_jsonrpc_request",
            response_log_tag="odoo_jsonrpc",
            timeout=30.0,
        )
        self.settings = self.client.settings

    def _contact_center_lookup_base_url(self) -> str | None:
        """Lee la URL base del buscador de contact center desde la configuracion."""
        return (
            str(self.settings.contact_center_lookup_url or self.settings.onu_base_url or "").strip()
            or None
        )

    async def _fetch_contact_center_lookup(self, texto: str) -> dict:
        """Consulta el endpoint `/sql2` del buscador de contact center."""
        base_url = self._contact_center_lookup_base_url()
        if not base_url:
            return {}
        url = f"{base_url.rstrip('/')}/sql2"
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, params={"texto": texto})
            snippet = (response.text or "").strip()[:800]
            logger.info(
                "contact_center_lookup status=%s url=%s body=%s",
                response.status_code,
                str(response.url),
                snippet,
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}

    @staticmethod
    def _merge_contact_center_rows(rows: list[dict]) -> list[dict]:
        """Agrupa filas repetidas por código de contrato y conserva los campos útiles."""
        grouped: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            contract_code = str(row.get("contrato_codigo") or "").strip()
            if not contract_code:
                continue
            current = grouped.setdefault(contract_code, {})
            for key, value in row.items():
                if key not in current or _placeholder(current.get(key)):
                    current[key] = value
        return list(grouped.values())

    @staticmethod
    def _normalize_contact_center_name(name: str, cedula: str) -> str:
        """Quita la cédula pegada al final del nombre del cliente."""
        cleaned = " ".join(str(name or "").split()).strip()
        suffix = f"- {cedula}".strip()
        if cleaned.endswith(suffix):
            return cleaned[: -len(suffix)].strip(" -")
        return cleaned

    @classmethod
    def _contact_center_contracts_from_payload(cls, payload: dict, cedula: str) -> list[dict]:
        """Convierte la respuesta del buscador en contratos con la forma que espera el orquestador."""
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        target = _digits(cedula)
        matches = [
            row
            for row in rows
            if isinstance(row, dict) and _digits(row.get("cliente_identificacion")) == target
        ]
        contracts: list[dict] = []
        for row in cls._merge_contact_center_rows(matches):
            contract_code = _pick_text(row.get("contrato_codigo"))
            if not contract_code:
                continue
            state_raw = _pick_text(row.get("contrato_estado")).lower()
            if "cort" in state_raw or "pend" in state_raw:
                state = "cortado"
            elif "activ" in state_raw or "open" in state_raw:
                state = "active"
            else:
                state = state_raw or "active"
            partner_name = cls._normalize_contact_center_name(
                _pick_text(row.get("cliente_nombre")),
                target,
            )
            email = _pick_text(row.get("cliente_email"))
            phone = _pick_text(row.get("celular"))
            address_line = " - ".join(
                part
                for part in (
                    _pick_text(row.get("localizacion_provincia")),
                    _pick_text(row.get("localizacion_ciudad")),
                    _pick_text(row.get("localizacion_direccion")),
                )
                if part
            )
            address_extra = " - ".join(
                part
                for part in (
                    _pick_text(row.get("localizacion_referencia")),
                    _pick_text(row.get("localizacion_sector")),
                )
                if part
            )
            plan_price = _format_decimal(row.get("precio_plan"))
            residual = plan_price if state == "cortado" else "0.00"
            contracts.append(
                {
                    "id": None,
                    "code": contract_code,
                    "state": state,
                    "street": address_line,
                    "street2": address_extra,
                    "phone": phone,
                    "email": email,
                    "residual": residual,
                    "plan": {
                        "name": _pick_text(row.get("plan_activo")),
                        "price": plan_price,
                    },
                    "partner_invoice": {"id": None, "name": partner_name},
                    "partner": {
                        "id": None,
                        "name": partner_name,
                        "dni": target,
                        "email": email,
                        "phone": phone,
                    },
                    "franchise": {
                        "id": None,
                        "name": _pick_text(row.get("compania_nombre")),
                        "deposit": [],
                    },
                    "source": "contact_center",
                }
            )
        return contracts

    async def _fallback_contracts_from_contact_center(self, cedula: str) -> list[dict]:
        """Intenta recuperar contratos por contact center cuando Odoo no responde."""
        base_url = self._contact_center_lookup_base_url()
        if not base_url:
            return []
        try:
            payload = await self._fetch_contact_center_lookup(cedula)
        except Exception:
            logger.exception("contact_center_lookup_failed cedula=%s", cedula)
            return []
        contracts = self._contact_center_contracts_from_payload(payload, cedula)
        logger.info(
            "contact_center_lookup_result cedula=%s count=%s base_url=%s",
            cedula,
            len(contracts),
            base_url,
        )
        return contracts

    async def _email_from_contact_center(self, cedula: str) -> str | None:
        """Saca el correo principal de la respuesta del buscador."""
        contracts = await self._fallback_contracts_from_contact_center(cedula)
        for contract in contracts:
            partner = contract.get("partner") if isinstance(contract.get("partner"), dict) else {}
            email = str(partner.get("email") or contract.get("email") or "").strip()
            if email and not _placeholder(email):
                return email
        return None

    @staticmethod
    def _mock_contracts(cedula: str) -> list[dict]:
        """Genera contratos sintéticos para pruebas o fallback."""
        suffix = (cedula or "0000")[-4:]
        return [
            {
                "id": 1001,
                "code": f"ACT-{suffix}",
                "state": "active",
                "partner_name": "Cliente Demo",
                "residual": "0.00",
                "partner": {"dni": cedula, "name": "Cliente Demo"},
                "partner_invoice": {"id": 9001},
                "franchise": {
                    "id": 77,
                    "deposit": [{"id": 11, "name": "BANCO PICHINCHA", "number": "2100223078", "code": "PICH"}],
                },
            },
            {
                "id": 1002,
                "code": f"CUT-{suffix}",
                "state": "cortado",
                "partner_name": "Cliente Demo",
                "residual": "18.50",
                "partner": {"dni": cedula, "name": "Cliente Demo"},
                "partner_invoice": {"id": 9002},
                "franchise": {
                    "id": 77,
                    "deposit": [{"id": 11, "name": "BANCO PICHINCHA", "number": "2100223078", "code": "PICH"}],
                },
            },
        ]

    async def _fetch_contract_lookup(
        self,
        cedula: str,
        *,
        refresh_franchises: bool,
        persist_franchises: bool,
    ) -> Any:
        """Devuelve el lookup fetch contract."""
        result = await self.client.execute(
            "res.partner",
            "get_contract_value",
            1,
            cedula,
            refresh_franchises,
            persist_franchises,
        )
        return _normalize_contract_lookup_result(result)

    async def info_personal_by_cedula(self, cedula: str) -> dict:
        """Devuelve la cedula info personal by."""
        if not self.client.is_configured():
            fallback_contracts = await self._fallback_contracts_from_contact_center(cedula)
            if fallback_contracts:
                return {"ok": True, "data": fallback_contracts, "source": "contact_center"}
            if self.settings.mock_mode:
                logger.info("info_personal_by_cedula_mock cedula=%s", cedula)
                return {"ok": True, "data": self._mock_contracts(cedula), "source": "mock"}
            logger.warning("info_personal_by_cedula_not_configured cedula=%s", cedula)
            return {"ok": False, "data": [], "error": "not_configured"}
        odoo_failed = False
        try:
            attempts = len(CONTRACT_LOOKUP_RETRY_DELAYS) + 1
            result: Any = []
            for attempt in range(1, attempts + 1):
                result = await self._fetch_contract_lookup(
                    cedula,
                    refresh_franchises=False,
                    persist_franchises=False,
                )
                logger.info(
                    "contract_lookup_attempt cedula=%s attempt=%s refresh_franchises=%s persist_franchises=%s count=%s",
                    cedula,
                    attempt,
                    False,
                    False,
                    _result_size(result),
                )
                if result:
                    break
                if attempt < attempts:
                    await asyncio.sleep(CONTRACT_LOOKUP_RETRY_DELAYS[attempt - 1])
            if not result:
                result = await self._fetch_contract_lookup(
                    cedula,
                    refresh_franchises=True,
                    persist_franchises=True,
                )
                logger.info(
                    "contract_lookup_attempt cedula=%s attempt=%s refresh_franchises=%s persist_franchises=%s count=%s",
                    cedula,
                    attempts + 1,
                    True,
                    True,
                    _result_size(result),
                )
            if not result:
                result = await self._fetch_contract_lookup(
                    cedula,
                    refresh_franchises=False,
                    persist_franchises=False,
                )
                logger.info(
                    "contract_lookup_attempt cedula=%s attempt=%s refresh_franchises=%s persist_franchises=%s count=%s",
                    cedula,
                    attempts + 2,
                    False,
                    False,
                    _result_size(result),
                )
        except Exception:
            logger.exception("info_personal_by_cedula_failed cedula=%s", cedula)
            odoo_failed = True
            result = []
        if not result:
            fallback_contracts = await self._fallback_contracts_from_contact_center(cedula)
            if fallback_contracts:
                return {"ok": True, "data": fallback_contracts, "source": "contact_center"}
        if odoo_failed:
            return {"ok": False, "data": [], "error": "lookup_failed"}
        return {"ok": True, "data": result}

    async def get_email_by_cedula(self, cedula: str) -> str | None:
        """Devuelve email by cedula."""
        if not self.client.is_configured():
            if self.settings.mock_mode:
                return f"demo+{cedula[-4:]}@gonet.test"
            return await self._email_from_contact_center(cedula)
        try:
            result = await self.client.execute_kw(
                "res.partner",
                "search_read",
                args=[[["vat", "=", cedula]]],
                kwargs={"fields": ["email"], "limit": 1},
            )
        except Exception:
            logger.exception("get_email_by_cedula_failed cedula=%s", cedula)
            return await self._email_from_contact_center(cedula)
        if result and isinstance(result, list):
            email = str(result[0].get("email") or "").strip()
            if email and not _placeholder(email):
                return email
        return await self._email_from_contact_center(cedula)
