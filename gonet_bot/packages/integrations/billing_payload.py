"""Construcción del payload que se envía al sistema de facturación."""

import base64
import json
import logging
import re
import unicodedata
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from packages.integrations.billing_franchise import ODOO_RPC_ERRORS
from packages.integrations.runtime import get_shared_http_client

logger = logging.getLogger("billing_registration")
ATTACHMENT_DOWNLOAD_ERRORS = (httpx.HTTPError, ValueError)


class BillingPayloadMixin:
    """Agrupa la lógica de facturación dentro del flujo de contacto."""
    
    @staticmethod
    def _requires_meta_bearer(url: str) -> bool:
        """Detecta si la URL apunta a un host de Meta que exige Bearer."""
        host = (urlparse(url).netloc or "").lower()
        return host in {
            "lookaside.fbsbx.com",
            "graph.facebook.com",
            "graph.facebook.net",
        }

    def _download_headers(self, url: str) -> dict[str, str]:
        """Construye los headers de descarga cuando la URL exige token de Meta."""
        if not self._requires_meta_bearer(url):
            return {}
        if not self.settings.whatsapp_media_token:
            return {}
        return {"Authorization": f"Bearer {self.settings.whatsapp_media_token}"}

    @staticmethod
    def _clean_base64(value: str) -> str:
        """Elimina el prefijo `data:` y compacta el base64."""
        if "," in value and value.lstrip().startswith("data:"):
            return value.split(",", 1)[1]
        return "".join(value.split())

    @staticmethod
    def _normalize_key(value: str) -> str:
        """Normaliza clave."""
        normalized = unicodedata.normalize("NFKD", str(value or ""))
        ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
        ascii_value = re.sub(r"[^a-z0-9]+", "_", ascii_value)
        return ascii_value.strip("_")

    @staticmethod
    def _normalize_text(value: str | None) -> str:
        """Normaliza texto."""
        normalized = unicodedata.normalize("NFKD", str(value or ""))
        ascii_value = normalized.encode("ascii", "ignore").decode("ascii").upper()
        return re.sub(r"[^A-Z0-9]+", " ", ascii_value).strip()

    @staticmethod
    def _digits(value: str | None) -> str:
        """Extrae solo los dígitos de un valor."""
        return re.sub(r"\D", "", str(value or ""))

    @staticmethod
    def _sanitize_code(value: Any) -> str | None:
        """Devuelve el codigo sanitize."""
        if value in (None, "", False):
            return None
        text = str(value).strip()
        text = re.sub(r"\s+", "", text)
        text = re.sub(r"[^A-Za-z0-9/_-]", "", text)
        if len(text) < 3:
            return None
        return text[:128]

    @staticmethod
    def _parse_amount(value: Any) -> float | None:
        """Convierte un texto en un monto positivo."""
        if value in (None, "", False):
            return None
        if isinstance(value, (int, float)):
            amount = float(value)
            return amount if amount > 0 else None
        text = str(value).strip()
        match = re.search(r"-?\d[\d.,]*", text)
        if not match:
            return None
        number = match.group()
        if "." in number and "," in number:
            if number.rfind(",") > number.rfind("."):
                number = number.replace(".", "").replace(",", ".")
            else:
                number = number.replace(",", "")
        elif "," in number:
            decimals = number.rsplit(",", 1)[-1]
            number = number.replace(",", ".") if len(decimals) <= 2 else number.replace(",", "")
        elif "." in number:
            decimals = number.rsplit(".", 1)[-1]
            number = number if len(decimals) <= 2 else number.replace(".", "")
        try:
            amount = float(number)
        except ValueError:
            return None
        return amount if amount > 0 else None

    @classmethod
    def _extract_decimal_amount_candidates(cls, text: str) -> list[float]:
        """Busca montos decimales en un texto y devuelve candidatos sin duplicados."""
        candidates: list[float] = []
        seen: set[float] = set()
        for match in re.finditer(
            r"(?<!\d)(?:USD\s*)?\$?\s*(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2}))(?!\d)",
            text,
            flags=re.IGNORECASE,
        ):
            amount = cls._parse_amount(match.group(1))
            if amount is None or amount in seen:
                continue
            seen.add(amount)
            candidates.append(amount)
        return candidates

    @classmethod
    def _month_number(cls, value: str | None) -> int | None:
        """Convierte un nombre de mes en su número."""
        normalized = unicodedata.normalize("NFKD", str(value or ""))
        ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
        ascii_value = re.sub(r"[^a-z]+", "", ascii_value)
        return cls.MONTH_ALIASES.get(ascii_value)

    @classmethod
    def _parse_date(cls, value: Any) -> str | None:
        """Interpreta una fecha escrita en varios formatos y la devuelve en ISO."""
        if value in (None, "", False):
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        text = str(value).strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y"):
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed.date().isoformat()
            except ValueError:
                continue
        match = re.search(r"\b(\d{2})[/-](\d{2})[/-](\d{2,4})\b", text)
        if match:
            day, month, year = match.groups()
            if len(year) == 2:
                year = f"20{year}"
            try:
                parsed = date(int(year), int(month), int(day))
            except ValueError:
                return None
            return parsed.isoformat()
        patterns = [
            r"\b(\d{4})[/-]([A-Za-z]{3,})\.?[/-](\d{1,2})\b",
            r"\b(\d{1,2})[/-]([A-Za-z]{3,})\.?[/-](\d{4})\b",
            r"\b(\d{1,2})\s+([A-Za-z]{3,})\.?,?\s+(\d{4})\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            first, month_text, last = match.groups()
            month = cls._month_number(month_text)
            if not month:
                continue
            if len(first) == 4:
                year = int(first)
                day = int(last)
            else:
                day = int(first)
                year = int(last)
            try:
                parsed = date(year, month, day)
            except ValueError:
                continue
            return parsed.isoformat()
        return None

    @classmethod
    def _ocr_date_diff_days(cls, payment_date: str | None) -> int | None:
        """Calcula los días transcurridos desde la fecha OCR."""
        if not payment_date:
            return None
        try:
            parsed = date.fromisoformat(str(payment_date))
        except ValueError:
            return None
        return abs((date.today() - parsed).days)

    @staticmethod
    def _balance_due(*, pending_value: float, amount: float | None) -> float:
        """Calcula el saldo pendiente tras aplicar un pago."""
        if amount is None:
            return float(pending_value)
        return max(float(pending_value) - float(amount), 0.0)

    @classmethod
    def _can_reconnect(cls, *, amount: float | None, pending_value: float) -> bool:
        """Decide si el saldo permite reconectar sin exceder la tolerancia."""
        return cls._balance_due(pending_value=pending_value, amount=amount) <= (cls.RECONNECT_TOLERANCE + 1e-6)

    def can_register_from_ocr(self, *, contract: dict, ocr_result: dict) -> bool:
        """Indica si el OCR ya trae datos suficientes para intentar registrar el pago."""
        if not isinstance(ocr_result, dict):
            return False
        if not self._extract_code(ocr_result):
            return False
        if self._extract_amount(ocr_result) is None:
            return False
        if not self._extract_date(ocr_result):
            return False
        if not self._select_deposit_bank(contract or {}, ocr_result):
            return False
        return True

    def can_override_retry_from_ocr(self, *, contract: dict, ocr_result: dict) -> bool:
        """Permite ignorar un retry solo en casos conservadores y con motivos seguros."""
        if not isinstance(ocr_result, dict):
            return False
        if not self._extract_code(ocr_result):
            return False
        if self._extract_amount(ocr_result) is None:
            return False
        if not self._extract_date(ocr_result):
            return False
        reason_values = (ocr_result.get("motivos_reintento") or ocr_result.get("retry_reasons") or [])
        reasons = {str(reason).strip().lower() for reason in reason_values if str(reason).strip()}
        if not reasons:
            return False
        return reasons == {"faltan_campos_criticos"}

    @staticmethod
    def _is_partial_payment(*, amount: float | None, pending_value: float) -> bool:
        """Detecta si el pago cubre solo una parte del saldo."""
        if amount is None:
            return False
        return float(amount) + 1e-6 < float(pending_value)

    @staticmethod
    def _invoice_name(invoice: dict) -> str:
        """Devuelve el nombre invoice."""
        invoice_id = invoice.get("id")
        for key in ("number", "name", "reference"):
            value = invoice.get(key)
            if value not in (None, "", False, "/"):
                return str(value)
        return f"INV-{invoice_id}" if invoice_id not in (None, "") else "INV"

    @classmethod
    def _masked_account_hints(cls, value: str | None) -> list[tuple[str, str]]:
        """Busca pistas de cuentas enmascaradas en un texto."""
        text = str(value or "")
        hints: list[tuple[str, str]] = []
        for prefix, suffix in re.findall(r"(\d{2,})\s*[Xx\\*#._-]{2,}\s*(\d{2,})", text):
            if prefix and suffix:
                hints.append((prefix, suffix))
        return hints

    @classmethod
    def _masked_account_sequences(cls, value: str | None) -> list[tuple[str, ...]]:
        """Busca secuencias numéricas en cuentas enmascaradas."""
        text = str(value or "")
        sequences: list[tuple[str, ...]] = []
        for masked_value in re.findall(r"(?:\d+[Xx\\*#._-]+){1,}\d+", text):
            chunks = tuple(chunk for chunk in re.findall(r"\d+", masked_value) if chunk)
            if len(chunks) >= 2:
                sequences.append(chunks)
        return sequences

    @staticmethod
    def _matches_masked_sequence(number: str, sequence: tuple[str, ...]) -> bool:
        """Comprueba si un número real coincide con una secuencia enmascarada."""
        if not number or len(sequence) < 2:
            return False
        start = 0
        for chunk in sequence:
            idx = number.find(chunk, start)
            if idx < 0:
                return False
            start = idx + len(chunk)
        return True

    @classmethod
    def _iter_key_values(cls, data: Any):
        """Devuelve los values iter clave."""
        if isinstance(data, dict):
            for key, value in data.items():
                yield cls._normalize_key(key), value
                yield from cls._iter_key_values(value)
        elif isinstance(data, list):
            for item in data:
                yield from cls._iter_key_values(item)

    @classmethod
    def _find_first_value(cls, data: Any, keys: set[str]) -> Any:
        """Devuelve el valor find first."""
        for key, value in cls._iter_key_values(data):
            if key in keys and isinstance(value, (str, int, float, date, datetime)) and value not in (None, "", False):
                return value
        return None

    @classmethod
    def _collect_scalar_texts(cls, data: Any) -> list[str]:
        """Devuelve el texts collect scalar."""
        values: list[str] = []

        def _walk(node: Any) -> None:
            """Devuelve el walk."""
            if isinstance(node, dict):
                for value in node.values():
                    _walk(value)
                return
            if isinstance(node, list):
                for item in node:
                    _walk(item)
                return
            if node in (None, "", False):
                return
            if isinstance(node, (str, int, float)):
                values.append(str(node))

        _walk(data)
        return values

    @classmethod
    def _deposit_bank_aliases(cls, deposit: dict) -> list[str]:
        """Devuelve los aliases deposit bank."""
        name = cls._normalize_text(deposit.get("name"))
        aliases: list[str] = []
        for token, hints in cls.BANK_ALIAS_HINTS.items():
            if token in name:
                aliases.extend(hints)
        return aliases

    @staticmethod
    def _ocr_payload(ocr_result: dict) -> dict:
        """Devuelve el payload ocr."""
        raw = ocr_result.get("raw")
        if isinstance(raw, dict) and raw:
            return raw
        return ocr_result if isinstance(ocr_result, dict) else {}

    async def _enrich_contract_for_registration(self, contract: dict) -> dict:
        """Devuelve el registration enrich contract for."""
        if not isinstance(contract, dict):
            return {}
        enriched = dict(contract)
        franchise = dict(enriched.get("franchise") or {})
        source = str(enriched.get("source") or "").strip().lower()

        franchise_id = franchise.get("id")
        if not franchise_id:
            franchise_name = franchise.get("name") or enriched.get("company") or enriched.get("company_name")
            matched_franchise = await self._find_franchise_by_name(franchise_name)
            if matched_franchise:
                franchise["id"] = matched_franchise.get("id")
                franchise.setdefault("name", matched_franchise.get("name"))
                franchise.setdefault("code", matched_franchise.get("code"))

        if franchise.get("id") and not (franchise.get("deposit") or []):
            franchise["deposit"] = await self._fetch_franchise_deposits(int(franchise["id"]))

        if source == "contact_center" and not enriched.get("id") and enriched.get("code"):
            enriched["id"] = str(enriched.get("code"))

        enriched["franchise"] = franchise
        return enriched

    def _extract_code_from_raw(self, raw: dict) -> str | None:
        """Extrae code from raw."""
        ordered_key_groups = [
            {"documento", "documento_numero", "doc", "doc_numero", "numero_documento", "codigo_cnb"},
            {"referencia", "reference", "numero_referencia"},
            {
                "numero_transaccion",
                "nro_transaccion",
                "nro_operacion",
                "numero_operacion",
                "operation_number",
                "transaction_id",
                "transaction_number",
                "transfer_number",
            },
            self.CODE_KEYS,
        ]
        for keys in ordered_key_groups:
            direct_value = self._find_first_value(raw, keys)
            code = self._sanitize_code(direct_value)
            if code:
                return code
        return None

    def _extract_code(self, ocr_result: dict) -> str | None:
        """Extrae code."""
        raw = self._ocr_payload(ocr_result)
        code = self._extract_code_from_raw(raw)
        if code:
            return code
        text = str(ocr_result.get("texto_extraido") or "")
        patterns = [
            r"(?:documento|doc)\s*[:#._-]*\s*([A-Z0-9/_-]*\d[A-Z0-9/_-]*)",
            r"(?:referencia|reference)\s*[:#._-]*\s*([A-Z0-9/_-]*\d[A-Z0-9/_-]*)",
            r"(?:transaccion|transaccion nro|transaccion no|transaccion numero|transacci[oó]n|operaci[oó]n|dep[oó]sito|deposito|comprobante|c[oó]digo)\s*[:#._-]*\s*([A-Z0-9/_-]*\d[A-Z0-9/_-]*)",
            r"\b([A-Z]{2,}\d{4,}[A-Z0-9/_-]*)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                code = self._sanitize_code(match.group(1))
                if code:
                    return code
        return None

    def _extract_amount(self, ocr_result: dict) -> float | None:
        """Extrae amount."""
        raw = self._ocr_payload(ocr_result)
        direct_value = self._find_first_value(raw, self.AMOUNT_KEYS)
        amount = self._parse_amount(direct_value)
        if amount is not None:
            return amount
        text = str(ocr_result.get("texto_extraido") or "")
        match = re.search(
            r"(?:monto|valor|total|pagado|depositado|efectivo)\D{0,20}(\d[\d.,]*)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return self._parse_amount(match.group(1))
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if re.fullmatch(r"\$?\s*\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})", stripped):
                amount = self._parse_amount(stripped)
                if amount is not None:
                    return amount
        decimal_candidates = self._extract_decimal_amount_candidates(text)
        if decimal_candidates:
            return max(decimal_candidates)
        return None

    def _extract_date(self, ocr_result: dict) -> str | None:
        """Extrae date."""
        raw = self._ocr_payload(ocr_result)
        direct_value = self._find_first_value(raw, self.DATE_KEYS)
        payment_date = self._parse_date(direct_value)
        if payment_date:
            return payment_date
        return self._parse_date(str(ocr_result.get("texto_extraido") or ""))

    def _select_deposit_bank(self, contract: dict, ocr_result: dict) -> dict | None:
        """Devuelve el bank select deposit."""
        deposits = ((contract.get("franchise") or {}).get("deposit")) or []
        if not deposits:
            return None
        if len(deposits) == 1:
            return deposits[0]
        raw = self._ocr_payload(ocr_result)
        bank_value = self._find_first_value(raw, self.BANK_KEYS)
        account_value = self._find_first_value(raw, self.ACCOUNT_KEYS)
        text_fragments = [
            str(ocr_result.get("texto_extraido") or ""),
            str(bank_value or ""),
            str(account_value or ""),
            *self._collect_scalar_texts(raw),
        ]
        masked_account_hints = self._masked_account_hints(" ".join(text_fragments))
        masked_account_sequences = self._masked_account_sequences(" ".join(text_fragments))
        normalized_text = self._normalize_text(" ".join(text_fragments))
        digits_text = self._digits(" ".join(text_fragments))

        best_match = None
        best_score = 0
        for deposit in deposits:
            score = 0
            name = self._normalize_text(deposit.get("name"))
            code = self._normalize_text(deposit.get("code"))
            number = self._digits(deposit.get("number"))
            aliases = self._deposit_bank_aliases(deposit)
            if any(alias in normalized_text for alias in aliases):
                score = max(score, 98)
            if number and number in digits_text:
                score = max(score, 100)
            elif number and len(number) >= 4 and number[-4:] in digits_text:
                score = max(score, 90)
            elif number and any(number.startswith(prefix) and number.endswith(suffix) for prefix, suffix in masked_account_hints):
                score = max(score, 95)
            elif number and any(self._matches_masked_sequence(number, sequence) for sequence in masked_account_sequences):
                score = max(score, 95)
            if name and name in normalized_text:
                score = max(score, 80)
            elif name:
                name_tokens = [token for token in name.split() if len(token) > 2]
                if name_tokens and all(token in normalized_text for token in name_tokens):
                    score = max(score, 70)
            if code and code in normalized_text:
                score = max(score, 60)
            if deposit.get("is_collection") and "RECAUDACION" in normalized_text:
                score = max(score, 85)
            if score > best_score:
                best_match = deposit
                best_score = score
        return best_match if best_score >= 60 else None

    async def _attachment_base64(self, attachment: dict) -> str:
        """Devuelve el base64 adjunto."""
        base64_data = attachment.get("base64_data")
        if base64_data:
            return self._clean_base64(base64_data)
        url = attachment.get("url")
        if not url:
            raise ValueError("attachment_missing_source")
        client = await get_shared_http_client()
        response = await client.get(url, headers=self._download_headers(url), timeout=60.0)
        response.raise_for_status()
        return base64.b64encode(response.content).decode("ascii")

    @staticmethod
    def _selected_dni(contract: dict, cedula: str | None) -> str | None:
        """Devuelve el dni selected."""
        partner = contract.get("partner") if isinstance(contract.get("partner"), dict) else {}
        values = [cedula, partner.get("dni"), partner.get("vat"), contract.get("dni"), contract.get("cedula")]
        for value in values:
            digits = re.sub(r"\D", "", str(value or ""))
            if digits:
                return digits
        return None

    async def _prepare_payload(
        self,
        *,
        contract: dict,
        ocr_result: dict,
        attachments: list[dict],
        cedula: str | None = None,
    ) -> dict:
        """Devuelve el payload prepare."""
        contract = await self._enrich_contract_for_registration(contract)
        missing: list[str] = []
        source = str(contract.get("source") or "").strip().lower()
        franchise = contract.get("franchise") if isinstance(contract.get("franchise"), dict) else {}
        partner_invoice = contract.get("partner_invoice") if isinstance(contract.get("partner_invoice"), dict) else {}
        franchise_id = franchise.get("id")
        contract_id = contract.get("id") or contract.get("code") or contract.get("contract")
        contract_code = contract.get("code") or contract.get("contract")
        partner_invoice_id = partner_invoice.get("id")
        pending_value = self._parse_amount(contract.get("residual")) or 0.0
        dni = self._selected_dni(contract, cedula)
        local_partner_id = await self._find_local_partner_id(dni)
        limited_registration = source == "contact_center" and not partner_invoice_id

        if not franchise_id:
            missing.append("franchise_id")
        if not dni:
            missing.append("dni")
        if not contract_id:
            missing.append("contract_id")
        if not contract_code:
            missing.append("contract")
        if not partner_invoice_id and not limited_registration:
            missing.append("partner_invoice_id")

        if not attachments:
            missing.append("image")
            image_base64 = None
        else:
            try:
                image_base64 = await self._attachment_base64(attachments[0])
            except ATTACHMENT_DOWNLOAD_ERRORS:
                logger.exception("billing_attachment_failed")
                image_base64 = None
                missing.append("image")

        code = self._extract_code(ocr_result)
        if not code:
            missing.append("code")

        deposit = self._select_deposit_bank(contract, ocr_result)
        if not deposit:
            missing.append("deposit")

        amount = self._extract_amount(ocr_result)
        if amount is None:
            missing.append("value")

        detected_date = self._extract_date(ocr_result)
        payment_date = detected_date or date.today().isoformat()
        date_diff_days = self._ocr_date_diff_days(detected_date)
        balance_due = self._balance_due(pending_value=pending_value, amount=amount)

        invoices: list[dict] = []
        if limited_registration:
            invoices = []
        elif not any(field in missing for field in {"franchise_id", "partner_invoice_id", "contract_id"}):
            try:
                invoices = await self._fetch_pending_invoices(
                    franchise_id=int(franchise_id),
                    partner_invoice_id=int(partner_invoice_id),
                    contract_id=int(contract_id),
                )
            except ODOO_RPC_ERRORS:
                logger.exception(
                    "billing_invoices_failed franchise_id=%s partner_invoice_id=%s contract_id=%s",
                    franchise_id,
                    partner_invoice_id,
                    contract_id,
                )
                missing.append("invoices")
        if not invoices and not limited_registration:
            missing.append("invoices")

        invoice_values = []
        invoice_names = []
        for invoice in invoices:
            invoice_id = invoice.get("id")
            invoice_name = self._invoice_name(invoice)
            invoice_value = self._parse_amount(invoice.get("residual")) or 0.0
            if invoice_id in (None, "") or not invoice_name:
                continue
            invoice_names.append(invoice_name)
            invoice_values.append(
                {
                    "invoice_id": str(invoice_id),
                    "name": invoice_name,
                    "value": invoice_value,
                }
            )

        create_values = None
        contract_values = None
        if not missing:
            create_values = {
                "franchise_id": int(franchise_id),
                "partner_id": local_partner_id or False,
                "dni": dni,
                "contract": str(contract_code),
                "name": code,
                "date": payment_date,
                "deposit_id": int(deposit["id"]),
                "pending_value": pending_value,
                "value": amount,
                "image": image_base64,
                "state": "draft",
            }
            contract_values = {
                "franchise_id": int(franchise_id),
                "partner_id": local_partner_id or False,
                "json": json.dumps(contract, ensure_ascii=False),
                "name": str(contract_code),
                "contract_id": str(contract_id),
                "invoices": ",".join(invoice_names),
                "value": pending_value,
                "state": "draft",
            }

        return {
            "missing": missing,
            "create_values": create_values,
            "contract_values": contract_values,
            "invoice_values": invoice_values,
            "resolved": {
                "franchise_id": franchise_id,
                "code": code,
                "deposit": deposit,
                "date": payment_date,
                "ocr_date": detected_date,
                "date_diff_days": date_diff_days,
                "max_ocr_date_diff_days": self.MAX_OCR_DATE_DIFF_DAYS,
                "value": amount,
                "pending_value": pending_value,
                "balance_due": balance_due,
                "reconnect_tolerance": self.RECONNECT_TOLERANCE,
                "dni": dni,
                "contract_id": contract_id,
                "contract": contract_code,
                "partner_invoice_id": partner_invoice_id,
                "partner_id": local_partner_id,
                "invoice_count": len(invoice_values),
                "limited_registration": limited_registration,
                "source": source or None,
            },
        }
