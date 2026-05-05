"""Consulta y normalización de franquicias de facturación."""

import asyncio
import base64
import json
import logging
import re
from binascii import Error as BinasciiError
from datetime import datetime
from json import JSONDecodeError
from urllib.parse import urljoin, urlparse, urlunparse
from xmlrpc.client import Fault, ProtocolError, ServerProxy

import httpx

logger = logging.getLogger("billing_registration")
ODOO_RPC_ERRORS = (httpx.HTTPError, JSONDecodeError, RuntimeError)
FRANCHISE_REMOTE_ERRORS = (Fault, ProtocolError, OSError, RuntimeError)


class BillingFranchiseMixin:
    """Agrupa la lógica de facturación dentro del flujo de contacto."""
    
    @classmethod
    def _company_name_terms(cls, value: str | None) -> list[str]:
        """Devuelve el terms company nombre."""
        raw = " ".join(str(value or "").split()).strip()
        normalized = cls._normalize_text(raw)
        tokens = [
            token
            for token in normalized.split()
            if len(token) > 3 and token not in {"CIA", "LTDA", "LIMITADA", "COMPANIA", "COMPANIAS"}
        ]
        terms: list[str] = []
        for candidate in (raw, normalized, " ".join(tokens[:2]), tokens[0] if tokens else ""):
            text = str(candidate or "").strip()
            if text and text not in terms:
                terms.append(text)
        return terms

    async def _find_franchise_by_name(self, name: str | None) -> dict | None:
        """Devuelve el nombre find franchise by."""
        for term in self._company_name_terms(name):
            try:
                rows = await self._execute_kw(
                    "app.gonet.franchise",
                    "search_read",
                    args=[[["name", "ilike", term]]],
                    kwargs={"fields": ["id", "name", "code"], "limit": 10},
                )
            except ODOO_RPC_ERRORS:
                logger.exception("billing_franchise_lookup_failed term=%s", term)
                continue
            if isinstance(rows, list) and rows:
                row = rows[0]
                return row if isinstance(row, dict) else None
        return None

    async def _fetch_franchise_deposits(self, franchise_id: int) -> list[dict]:
        """Devuelve el deposits fetch franchise."""
        try:
            rows = await self._execute_kw(
                "app.gonet.franchise.deposit",
                "search_read",
                args=[[["franchise_id", "=", int(franchise_id)]]],
                kwargs={"fields": ["id", "name", "number", "code", "franchise_id", "is_collection"], "limit": 50},
            )
        except ODOO_RPC_ERRORS:
            logger.exception("billing_franchise_deposits_failed franchise_id=%s", franchise_id)
            return []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def _franchise_aes_key(self) -> bytes:
        """Devuelve el clave franchise aes."""
        key = str(self.settings.franchise_aes_key or "").encode("utf-8")
        if not key:
            raise RuntimeError("franchise_aes_key_not_configured")
        if len(key) not in {16, 24, 32}:
            raise RuntimeError("franchise_aes_key_invalid_length")
        return key

    def _franchise_aes_iv(self) -> bytes:
        """Devuelve el iv franchise aes."""
        raw_iv = str(self.settings.franchise_aes_iv_base64 or "").strip()
        if not raw_iv:
            raise RuntimeError("franchise_aes_iv_not_configured")
        try:
            iv = base64.b64decode(raw_iv, validate=True)
        except (BinasciiError, ValueError) as exc:
            raise RuntimeError("franchise_aes_iv_invalid_base64") from exc
        if len(iv) != 16:
            raise RuntimeError("franchise_aes_iv_invalid_length")
        return iv

    def _decrypt_franchise_value(self, value: str | None) -> str | None:
        """Devuelve el valor decrypt franchise."""
        if value in (None, "", False):
            return None
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError as exc:  # pragma: no cover - exercised in container/runtime
            raise RuntimeError("franchise_crypto_unavailable") from exc

        ciphertext = base64.b64decode(str(value))
        cipher = Cipher(
            algorithms.AES(self._franchise_aes_key()),
            modes.CTR(self._franchise_aes_iv()),
        )
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return plaintext.decode()

    @staticmethod
    def _parse_franchise_host_map(raw: str | None) -> dict[str, str]:
        """Devuelve el map parse franchise host."""
        mappings: dict[str, str] = {}
        for item in re.split(r"[\n,;]+", str(raw or "")):
            entry = item.strip()
            if not entry or "=" not in entry:
                continue
            source, target = entry.split("=", 1)
            source = source.strip().lower()
            target = target.strip()
            if source and target:
                mappings[source] = target
        return mappings

    def _normalize_franchise_url(self, value: str | None) -> str | None:
        """Normaliza URL franchise."""
        raw_url = str(value or "").strip()
        if not raw_url:
            return None
        parsed = urlparse(raw_url if "://" in raw_url else f"http://{raw_url}")
        overrides = self._parse_franchise_host_map(self.settings.franchise_xmlrpc_host_map)
        replacement = overrides.get((parsed.hostname or "").lower())
        if not replacement:
            return urlunparse(parsed)
        mapped = urlparse(replacement if "://" in replacement else f"{parsed.scheme or 'http'}://{replacement}")
        return urlunparse(
            (
                mapped.scheme or parsed.scheme or "http",
                mapped.netloc or mapped.path,
                mapped.path if mapped.netloc and mapped.path else parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )

    async def _fetch_franchise_remote_connection(self, franchise_id: int) -> dict:
        """Devuelve el connection fetch franchise remote."""
        rows = await self._execute_kw(
            "app.gonet.franchise",
            "search_read",
            args=[[["id", "=", int(franchise_id)]]],
            kwargs={"fields": ["id", "name", "is_new", "ip", "db", "user", "password"], "limit": 1},
        )
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, dict):
            raise RuntimeError(f"franchise_not_found:{franchise_id}")
        base_url = self._normalize_franchise_url(self._decrypt_franchise_value(row.get("ip")))
        database = self._decrypt_franchise_value(row.get("db"))
        username = self._decrypt_franchise_value(row.get("user"))
        password = self._decrypt_franchise_value(row.get("password"))
        if not all([base_url, database, username, password]):
            raise RuntimeError(f"franchise_connection_incomplete:{franchise_id}")
        return {
            "id": int(row["id"]),
            "name": row.get("name"),
            "is_new": bool(row.get("is_new")),
            "base_url": base_url,
            "db": database,
            "username": username,
            "password": password,
        }

    @staticmethod
    def _remote_common_url(base_url: str) -> str:
        """Devuelve el URL remote common."""
        return urljoin(base_url.rstrip("/") + "/", "xmlrpc/2/common")

    @staticmethod
    def _remote_object_url(base_url: str) -> str:
        """Devuelve el URL remote object."""
        return urljoin(base_url.rstrip("/") + "/", "xmlrpc/2/object")

    @staticmethod
    def _remote_contract_model(is_new: bool) -> tuple[str, str, str]:
        """Devuelve el model remote contract."""
        if is_new:
            return ("sale.subscription", "code", "state_service")
        return ("account.analytic.account", "name", "state")

    def _resolve_remote_contract_sync(self, connection: dict, contract_code: str) -> dict:
        """Resuelve sync remote contract."""
        common = ServerProxy(self._remote_common_url(connection["base_url"]), allow_none=True)
        uid = common.authenticate(connection["db"], connection["username"], connection["password"], {})
        if not uid:
            raise RuntimeError(f"franchise_remote_auth_failed:{connection['id']}")

        model, code_field, state_field = self._remote_contract_model(bool(connection.get("is_new")))
        proxy = ServerProxy(self._remote_object_url(connection["base_url"]), allow_none=True)
        rows = proxy.execute_kw(
            connection["db"],
            uid,
            connection["password"],
            model,
            "search_read",
            [[(code_field, "=", str(contract_code))]],
            {"fields": ["id", code_field, state_field], "limit": 1},
        )
        row = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(row, dict):
            raise RuntimeError(f"franchise_remote_contract_not_found:{contract_code}")

        remote_id = int(row["id"])
        if bool(connection.get("is_new")):
            values = {state_field: "open"}
        else:
            values = {
                state_field: "open",
                "date_update_state": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        current_state = str(row.get(state_field) or "").strip().lower()
        if current_state != "open":
            success = proxy.execute_kw(
                connection["db"],
                uid,
                connection["password"],
                model,
                "write",
                [[remote_id], values],
            )
            if success is False:
                raise RuntimeError(f"franchise_remote_reconnect_failed:{contract_code}")

        return {
            "id": remote_id,
            "model": model,
            "code": row.get(code_field) or contract_code,
            "state": "open",
            "state_field": state_field,
        }

    async def _reconnect_with_tolerance(
        self,
        *,
        deposit_id: int,
        local_contract_record_id: int | None,
        local_contract_json: str | None,
        resolved: dict,
    ) -> dict:
        """Devuelve el tolerance reconnect with."""
        franchise_id = resolved.get("franchise_id")
        contract_code = str(resolved.get("contract") or "").strip()
        if not franchise_id:
            raise RuntimeError("franchise_id_missing_for_tolerance_reconnect")
        if not contract_code:
            raise RuntimeError("contract_code_missing_for_tolerance_reconnect")

        connection = await self._fetch_franchise_remote_connection(int(franchise_id))
        remote_contract = await asyncio.to_thread(self._resolve_remote_contract_sync, connection, contract_code)

        await self._execute(
            "app.gonet.deposit",
            "write",
            [int(deposit_id)],
            {
                "state": "reconnect",
                "reconnect_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

        if local_contract_record_id:
            try:
                snapshot = json.loads(str(local_contract_json or "{}"))
            except JSONDecodeError:
                snapshot = {}
            if isinstance(snapshot, dict):
                snapshot["id"] = remote_contract["id"]
                snapshot["code"] = snapshot.get("code") or contract_code
                state_field = remote_contract.get("state_field")
                if state_field:
                    snapshot[state_field] = "open"
            try:
                await self._execute(
                    "app.gonet.contract",
                    "write",
                    [int(local_contract_record_id)],
                    {
                        "contract_id": str(remote_contract["id"]),
                        "json": json.dumps(snapshot, ensure_ascii=False),
                    },
                )
            except ODOO_RPC_ERRORS:
                logger.exception(
                    "billing_registration_contract_sync_failed deposit_id=%s contract_record_id=%s",
                    deposit_id,
                    local_contract_record_id,
                )

        logger.info(
            "billing_registration_tolerance_reconnect_done deposit_id=%s contract=%s franchise_id=%s remote_contract_id=%s",
            deposit_id,
            contract_code,
            franchise_id,
            remote_contract["id"],
        )
        return remote_contract
