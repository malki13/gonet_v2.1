"""Registro y validacion de pagos de facturacion."""

import json
import logging
from json import JSONDecodeError

from packages.integrations.billing_franchise import BillingFranchiseMixin, FRANCHISE_REMOTE_ERRORS, ODOO_RPC_ERRORS
from packages.integrations.billing_payload import BillingPayloadMixin
from packages.integrations.odoo_jsonrpc import OdooJsonRpcClient
from packages.shared.config import get_settings

logger = logging.getLogger("billing_registration")
TOLERANCE_RECONNECT_ERRORS = FRANCHISE_REMOTE_ERRORS + ODOO_RPC_ERRORS


class BillingRegistrationService(BillingFranchiseMixin, BillingPayloadMixin):
    """Servicio que coordina facturación y validaciones de identidad."""
    RECONNECT_TOLERANCE = 5.0
    MAX_OCR_DATE_DIFF_DAYS = 15
    MONTH_ALIASES = {
        "ene": 1,
        "enero": 1,
        "jan": 1,
        "january": 1,
        "feb": 2,
        "febrero": 2,
        "february": 2,
        "mar": 3,
        "marzo": 3,
        "march": 3,
        "abr": 4,
        "abril": 4,
        "apr": 4,
        "april": 4,
        "may": 5,
        "mayo": 5,
        "jun": 6,
        "junio": 6,
        "june": 6,
        "jul": 7,
        "julio": 7,
        "july": 7,
        "ago": 8,
        "agosto": 8,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "set": 9,
        "septiembre": 9,
        "setiembre": 9,
        "september": 9,
        "oct": 10,
        "octubre": 10,
        "october": 10,
        "nov": 11,
        "noviembre": 11,
        "november": 11,
        "dic": 12,
        "diciembre": 12,
        "dec": 12,
        "december": 12,
    }
    CODE_KEYS = {
        "codigo",
        "codigo_cnb",
        "codigo_pago",
        "code",
        "comprobante",
        "comprobante_numero",
        "comprobante_pago",
        "deposito",
        "deposit_number",
        "deposito_numero",
        "documento",
        "documento_numero",
        "numero",
        "numero_comprobante",
        "numero_deposito",
        "numero_documento",
        "numero_operacion",
        "numero_referencia",
        "numero_transaccion",
        "nro_comprobante",
        "nro_operacion",
        "nro_transaccion",
        "operation_number",
        "payment_code",
        "reference",
        "referencia",
        "sequence",
        "secuencia",
        "transaction_id",
        "transaction_number",
        "transfer_number",
    }
    AMOUNT_KEYS = {
        "amount",
        "amount_paid",
        "cash",
        "efectivo",
        "importe",
        "monto",
        "monto_pagado",
        "payment_amount",
        "total",
        "total_amount",
        "total_numeric",
        "valor",
        "valor_pagado",
        "value",
    }
    DATE_KEYS = {
        "date",
        "fecha",
        "fecha_de_pago",
        "fecha_pago",
        "payment_date",
        "transfer_date",
    }
    BANK_KEYS = {
        "bank",
        "bank_name",
        "banco",
        "banco_destino",
        "banco_receptor",
        "destination_bank",
        "entidad_bancaria",
        "entity",
        "institucion",
        "institucion_financiera",
    }
    ACCOUNT_KEYS = {
        "account",
        "account_number",
        "cuenta",
        "cuenta_destino",
        "cuenta_receptora",
        "destination_account",
        "numero_cuenta",
    }
    BANK_ALIAS_HINTS = {
        "PICHINCHA": ("BANCO PICHINCHA", "PICHINCHA"),
        "JEP": ("COOPERATIVA JEP", "JEP"),
        "GUAYAQUIL": ("BANCO DE GUAYAQUIL", "BANCO GUAYAQUIL", "GUAYAQUIL"),
        "MACHALA": ("BANCO MACHALA", "MACHALA"),
        "PACIFICO": ("BANCO DEL PACIFICO", "BANCO PACIFICO", "PACIFICO"),
    }

    def __init__(self) -> None:
        """Inicializa el billingregistrationservice con la configuracion necesaria."""
        self.settings = get_settings()
        self.client = OdooJsonRpcClient(
            logger=logger,
            request_log_tag="billing_registration_jsonrpc_request",
            response_log_tag="billing_registration_jsonrpc_response",
            timeout=30.0,
        )

    def _is_configured(self) -> bool:
        """Indica si hay datos suficientes para hablar con Odoo por JSON-RPC."""
        return self.client.is_configured()

    def partial_balance_followup(self, registration: dict | None) -> dict | None:
        """Resume un faltante significativo cuando el pago no cubre la deuda total."""
        if not isinstance(registration, dict) or registration.get("status") != "missing_fields":
            return None
        missing = {str(item).strip().lower() for item in (registration.get("missing") or []) if str(item).strip()}
        if missing != {"invoices"}:
            return None
        resolved = registration.get("resolved") or {}
        pending_value = self._parse_amount(resolved.get("pending_value"))
        paid_value = self._parse_amount(resolved.get("value"))
        balance_due = self._parse_amount(resolved.get("balance_due"))
        if pending_value is None or paid_value is None or balance_due is None:
            return None
        if balance_due <= self.RECONNECT_TOLERANCE + 1e-6:
            return None
        return {
            "pending_value": pending_value,
            "paid_value": paid_value,
            "balance_due": balance_due,
        }

    async def _execute(self, model: str, method: str, *method_args):
        """Ejecuta la operacion remota usando la configuracion actual."""
        return await self.client.execute(model, method, *method_args)

    async def _execute_kw(self, model: str, method: str, *, args=None, kwargs=None):
        """Ejecuta kw con la configuracion actual."""
        return await self.client.execute_kw(model, method, args=args or [], kwargs=kwargs or {})

    async def _find_local_partner_id(self, dni: str | None) -> int | None:
        """Devuelve el id find local partner."""
        dni_value = self._digits(dni)
        if not dni_value:
            return None
        try:
            result = await self._execute("res.partner", "search", [["vat", "=", dni_value]], 0, 1)
        except ODOO_RPC_ERRORS:
            logger.exception("billing_partner_lookup_failed dni=%s", dni_value)
            return None
        if isinstance(result, list) and result:
            try:
                return int(result[0])
            except (TypeError, ValueError):
                return None
        return None

    async def _fetch_pending_invoices(self, *, franchise_id: int, partner_invoice_id: int, contract_id: int) -> list[dict]:
        """Devuelve los invoices fetch pending."""
        result = await self._execute(
            "res.partner",
            "get_invoices_contract",
            1,
            franchise_id,
            partner_invoice_id,
            contract_id,
            True,
        )
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except JSONDecodeError:
                return []
            return parsed if isinstance(parsed, list) else []
        return result if isinstance(result, list) else []

    async def _find_existing_deposit(self, code: str | None) -> dict | None:
        """Devuelve el deposit find existing."""
        code_value = self._sanitize_code(code)
        if not code_value:
            return None
        try:
            rows = await self._execute_kw(
                "app.gonet.deposit",
                "search_read",
                args=[[["name", "=", code_value]]],
                kwargs={
                    "fields": ["id", "name", "state", "dni", "contract", "date", "pending_value", "value"],
                    "limit": 1,
                },
            )
        except ODOO_RPC_ERRORS:
            logger.exception("billing_duplicate_lookup_failed code=%s", code_value)
            return None
        if isinstance(rows, list) and rows:
            row = rows[0]
            return row if isinstance(row, dict) else None
        return None

    async def register_payment(
        self,
        *,
        contract: dict,
        ocr_result: dict,
        attachments: list[dict],
        cedula: str | None = None,
    ) -> dict:
        """Devuelve el payment register."""
        if not self._is_configured():
            if self.settings.mock_mode:
                pending_value = self._parse_amount(contract.get("residual")) or 0.0
                amount = self._parse_amount((ocr_result or {}).get("valor")) or pending_value or 18.5
                balance_due = self._balance_due(pending_value=pending_value, amount=amount)
                reconnect_status = "done" if balance_due <= self.RECONNECT_TOLERANCE else "pending_balance"
                return {
                    "status": "created",
                    "deposit_id": 99001,
                    "resolved": {
                        "code": str((ocr_result or {}).get("documento") or "DEMO123"),
                        "value": amount,
                        "pending_value": pending_value,
                        "balance_due": balance_due,
                        "reconnect_status": reconnect_status,
                        "deposit": {"name": str((ocr_result or {}).get("bank") or "BANCO PICHINCHA")},
                        "mock": True,
                    },
                }
            logger.warning("billing_registration_unconfigured")
            return {"status": "unconfigured"}

        prepared = await self._prepare_payload(
            contract=contract,
            ocr_result=ocr_result,
            attachments=attachments,
            cedula=cedula,
        )
        date_diff_days = prepared["resolved"].get("date_diff_days")
        if date_diff_days is not None and int(date_diff_days) > self.MAX_OCR_DATE_DIFF_DAYS:
            return {"status": "date_out_of_range", "resolved": prepared["resolved"]}

        existing_deposit = await self._find_existing_deposit(prepared["resolved"].get("code"))
        if existing_deposit:
            prepared["resolved"]["existing_deposit"] = existing_deposit
            prepared["resolved"]["existing_deposit_id"] = existing_deposit.get("id")
            return {
                "status": "duplicate",
                "deposit_id": existing_deposit.get("id"),
                "resolved": prepared["resolved"],
            }

        if prepared["missing"]:
            missing = sorted(set(prepared["missing"]))
            return {"status": "missing_fields", "missing": missing, "resolved": prepared["resolved"]}

        try:
            deposit_id = await self._execute("app.gonet.deposit", "create", prepared["create_values"])
        except ODOO_RPC_ERRORS:
            existing_deposit = await self._find_existing_deposit(prepared["resolved"].get("code"))
            if existing_deposit:
                prepared["resolved"]["existing_deposit"] = existing_deposit
                prepared["resolved"]["existing_deposit_id"] = existing_deposit.get("id")
                return {
                    "status": "duplicate",
                    "deposit_id": existing_deposit.get("id"),
                    "resolved": prepared["resolved"],
                }
            logger.exception("billing_registration_create_failed resolved=%s", prepared["resolved"])
            return {"status": "error", "resolved": prepared["resolved"]}

        prepared["resolved"]["deposit_id"] = deposit_id
        try:
            contract_record_id = await self._execute(
                "app.gonet.contract",
                "create",
                {
                    **(prepared.get("contract_values") or {}),
                    "deposit_id": int(deposit_id),
                },
            )
            for invoice_value in prepared.get("invoice_values") or []:
                await self._execute(
                    "app.gonet.invoice",
                    "create",
                    {
                        **invoice_value,
                        "contract_id": int(contract_record_id),
                    },
                )
        except ODOO_RPC_ERRORS:
            logger.exception("billing_registration_contract_failed deposit_id=%s", deposit_id)
            return {"status": "error", "deposit_id": deposit_id, "resolved": prepared["resolved"]}

        pending_value = prepared["create_values"].get("pending_value") or 0.0
        amount = prepared["resolved"].get("value")
        balance_due = self._balance_due(pending_value=pending_value, amount=amount)
        reconnect_status = "pending_balance"
        reconnect_error = None

        if self._can_reconnect(amount=amount, pending_value=pending_value):
            use_direct_reconnect = bool(prepared["resolved"].get("limited_registration")) or self._is_partial_payment(
                amount=amount,
                pending_value=pending_value,
            )
            if use_direct_reconnect:
                try:
                    remote_contract = await self._reconnect_with_tolerance(
                        deposit_id=int(deposit_id),
                        local_contract_record_id=int(contract_record_id),
                        local_contract_json=(prepared.get("contract_values") or {}).get("json"),
                        resolved=prepared["resolved"],
                    )
                    reconnect_status = "done"
                    prepared["resolved"]["remote_contract_id"] = remote_contract.get("id")
                    prepared["resolved"]["remote_contract_model"] = remote_contract.get("model")
                except TOLERANCE_RECONNECT_ERRORS as exc:
                    reconnect_status = "error"
                    reconnect_error = str(exc)
                    logger.exception("billing_registration_tolerance_reconnect_failed deposit_id=%s", deposit_id)
            else:
                try:
                    await self._execute("app.gonet.deposit", "action_reconnect", [int(deposit_id)])
                    reconnect_status = "done"
                except ODOO_RPC_ERRORS as exc:
                    reconnect_status = "error"
                    reconnect_error = str(exc)
                    logger.exception("billing_registration_reconnect_failed deposit_id=%s", deposit_id)

        prepared["resolved"]["reconnect_status"] = reconnect_status
        prepared["resolved"]["balance_due"] = balance_due
        if reconnect_error:
            prepared["resolved"]["reconnect_error"] = reconnect_error
        return {"status": "created", "deposit_id": deposit_id, "resolved": prepared["resolved"]}
