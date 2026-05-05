"""Cliente JSON-RPC de bajo nivel para Odoo."""

import asyncio
import json
import logging

import httpx

from packages.shared.config import get_settings


class OdooJsonRpcClient:
    """Cliente JSON-RPC de bajo nivel para Odoo."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        request_log_tag: str,
        response_log_tag: str,
        timeout: float = 60.0,
    ):
        """Inicializa el cliente json-rpc de bajo nivel para odoo con autenticacion y logs de peticiones con la configuracion necesaria."""
        self.logger = logger
        self.request_log_tag = request_log_tag
        self.response_log_tag = response_log_tag
        self.timeout = timeout
        self.settings = get_settings()
        self._resolved_uid: int | None = None
        self._resolve_uid_lock = asyncio.Lock()

    def _url(self) -> str | None:
        """Devuelve la URL configurada para el endpoint JSON-RPC de Odoo."""
        return self.settings.odoo_jsonrpc_url or None

    def _db(self) -> str | None:
        """Devuelve la base de datos de Odoo tomada de la configuracion."""
        return self.settings.odoo_jsonrpc_db or self.settings.odoo_db or None

    def _username(self) -> str | None:
        """Devuelve el usuario de Odoo configurado para JSON-RPC."""
        return self.settings.odoo_jsonrpc_username or self.settings.odoo_username or None

    def _configured_uid(self) -> int | None:
        """Devuelve el UID fijo si está configurado."""
        uid = self.settings.odoo_jsonrpc_uid
        return uid if uid and uid > 0 else None

    def _password(self) -> str | None:
        """Devuelve la contraseña de Odoo configurada para JSON-RPC."""
        return self.settings.odoo_jsonrpc_password or self.settings.odoo_password or None

    def is_configured(self) -> bool:
        """Indica si hay datos suficientes para hablar con Odoo por JSON-RPC."""
        return all(
            [
                self._url(),
                self._db(),
                self._password(),
                self._username() or self._configured_uid() is not None,
            ]
        )

    @staticmethod
    def _scrub_sensitive(value):
        """Oculta credenciales y datos sensibles antes de registrar el payload."""
        if isinstance(value, dict):
            scrubbed = {}
            for key, item in value.items():
                if key == "image" and isinstance(item, str) and item:
                    scrubbed[key] = f"<base64 len={len(item)}>"
                elif key == "args" and isinstance(item, list):
                    scrubbed_args = [OdooJsonRpcClient._scrub_sensitive(arg) for arg in item]
                    if len(scrubbed_args) >= 3 and isinstance(scrubbed_args[2], str) and scrubbed_args[2]:
                        scrubbed_args[2] = "<redacted>"
                    scrubbed[key] = scrubbed_args
                elif key in {"password", "passwd"} and isinstance(item, str) and item:
                    scrubbed[key] = "<redacted>"
                else:
                    scrubbed[key] = OdooJsonRpcClient._scrub_sensitive(item)
            return scrubbed
        if isinstance(value, list):
            return [OdooJsonRpcClient._scrub_sensitive(item) for item in value]
        return value

    @staticmethod
    def _error_preview(error: dict) -> str:
        """Resume el error JSON-RPC de Odoo para dejarlo legible en logs."""
        message = str(error.get("message") or "Odoo JSON-RPC error").strip()
        data = error.get("data") or {}
        debug_lines = [line.strip() for line in str(data.get("debug") or "").splitlines() if line.strip()]
        if debug_lines and debug_lines[0].lower().startswith("traceback"):
            debug_lines = debug_lines[1:]
        preview_parts = debug_lines[-4:]
        if not preview_parts:
            fallback = str(data.get("message") or data.get("name") or "").strip()
            if fallback:
                preview_parts = [fallback]
        if not preview_parts:
            return message
        preview = " | ".join(preview_parts)
        if len(preview) > 1200:
            preview = preview[-1200:]
        return f"{message}: {preview}".strip()

    async def _jsonrpc(self, payload: dict) -> dict:
        """Ejecuta una petición JSON-RPC contra Odoo y devuelve la respuesta."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            self.logger.info(
                "%s=%s",
                self.request_log_tag,
                json.dumps(self._scrub_sensitive(payload), ensure_ascii=False),
            )
            response = await client.post(self._url(), json=payload, timeout=self.timeout)
            snippet = (response.text or "").strip()[:4000]
            self.logger.info(
                "%s status=%s url=%s body=%s",
                self.response_log_tag,
                response.status_code,
                str(response.url),
                snippet,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("error"):
                raise RuntimeError(self._error_preview(data["error"]))
            return data

    async def _resolve_uid(self) -> int | None:
        """Resuelve uid."""
        username = self._username()
        if not username:
            return self._configured_uid()
        if self._resolved_uid is not None:
            return self._resolved_uid

        async with self._resolve_uid_lock:
            if self._resolved_uid is not None:
                return self._resolved_uid

            payload = {
                "jsonrpc": "2.0",
                "method": "call",
                "params": {
                    "service": "common",
                    "method": "authenticate",
                    "args": [
                        self._db(),
                        username,
                        self._password(),
                        {},
                    ],
                },
                "id": 1,
            }
            data = await self._jsonrpc(payload)
            result = data.get("result")
            try:
                uid = int(result)
            except (TypeError, ValueError):
                uid = 0
            if uid <= 0:
                raise RuntimeError("odoo_jsonrpc_auth_failed")
            self._resolved_uid = uid
            return uid

    async def execute(self, model: str, method: str, *method_args):
        """Ejecuta el método remoto de Odoo con la sesión JSON-RPC actual."""
        uid = await self._resolve_uid()
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "object",
                "method": "execute",
                "args": [
                    self._db(),
                    uid,
                    self._password(),
                    model,
                    method,
                    *method_args,
                ],
            },
            "id": 1,
        }
        data = await self._jsonrpc(payload)
        return data.get("result")

    async def execute_kw(self, model: str, method: str, args=None, kwargs=None):
        """Ejecuta `execute_kw` sobre Odoo con args y kwargs."""
        uid = await self._resolve_uid()
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "object",
                "method": "execute_kw",
                "args": [
                    self._db(),
                    uid,
                    self._password(),
                    model,
                    method,
                    args or [],
                    kwargs or {},
                ],
            },
            "id": 1,
        }
        data = await self._jsonrpc(payload)
        return data.get("result")
