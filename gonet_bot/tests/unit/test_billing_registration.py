import base64
from datetime import date

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from packages.integrations.billing_registration import BillingRegistrationService
from packages.shared.config import get_settings


def test_extract_amount_from_transfer_text_with_currency_symbol():
    service = BillingRegistrationService()

    amount = service._extract_amount(
        {
            "texto_extraido": (
                "BANCO PICHINCHA\n"
                "Transferencia exitosa\n"
                "$54.98\n"
                "A Marvicnet Cia Ltda\n"
                "N de comprobante 61146151"
            ),
        }
    )

    assert amount == pytest.approx(54.98)


def test_select_deposit_bank_prefers_explicit_bank_mentioned_in_transfer_text():
    service = BillingRegistrationService()
    contract = {
        "franchise": {
            "deposit": [
                {
                    "id": 1,
                    "name": "BANCO PICHINCHA Cta. Cte. # 2100174952",
                    "code": "BPCH",
                    "number": "2100174952",
                    "is_collection": False,
                },
                {
                    "id": 3,
                    "name": "COOP. JEP Cta. Ahorro # 406101026309",
                    "code": "BCJEP",
                    "number": "406101026309",
                    "is_collection": False,
                },
            ]
        }
    }

    deposit = service._select_deposit_bank(
        contract,
        {
            "texto_extraido": (
                "BANCO PICHINCHA\n"
                "Transferencia exitosa\n"
                "$54.98\n"
                "A Marvicnet Cia Ltda\n"
                "Cuenta destino *** *** 4952\n"
                "Banco destino Banco Pichincha\n"
                "N de comprobante 61146151"
            ),
        },
    )

    assert deposit is not None
    assert deposit["id"] == 1


def test_decrypt_franchise_value_requires_crypto_configuration(monkeypatch):
    service = BillingRegistrationService()
    settings = get_settings()
    monkeypatch.setattr(settings, "franchise_aes_key", None)
    monkeypatch.setattr(settings, "franchise_aes_iv_base64", None)

    with pytest.raises(RuntimeError, match="franchise_aes_key_not_configured"):
        service._decrypt_franchise_value("ZGVtbw==")


def test_decrypt_franchise_value_uses_configured_crypto(monkeypatch):
    service = BillingRegistrationService()
    settings = get_settings()
    key = "56eRI0OC8JMk!@86MQrY^8TByEZ&JNsv"
    iv = base64.b64decode("Ylo2MUA3JmhtR0t1RThISw==")
    cipher = Cipher(algorithms.AES(key.encode("utf-8")), modes.CTR(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(b"demo-value") + encryptor.finalize()

    monkeypatch.setattr(settings, "franchise_aes_key", key)
    monkeypatch.setattr(settings, "franchise_aes_iv_base64", base64.b64encode(iv).decode("ascii"))

    assert service._decrypt_franchise_value(base64.b64encode(ciphertext).decode("ascii")) == "demo-value"


@pytest.mark.asyncio
async def test_register_payment_creates_deposit_record_directly(monkeypatch):
    service = BillingRegistrationService()
    captured = {"calls": []}

    async def fake_find_local_partner_id(dni: str | None):
        assert dni == "0912345678"
        return 4321

    async def fake_fetch_pending_invoices(*, franchise_id: int, partner_invoice_id: int, contract_id: int):
        assert franchise_id == 77
        assert partner_invoice_id == 654
        assert contract_id == 501
        return [
            {"id": 9001, "number": "FAC-001", "residual": 12.29},
            {"id": 9002, "number": "FAC-002", "residual": 10.00},
        ]

    async def fake_find_existing_deposit(code: str | None):
        return None

    async def fake_execute(model: str, method: str, *method_args):
        captured["calls"].append((model, method, method_args))
        if method == "create":
            return 840
        return True

    monkeypatch.setattr(service, "_is_configured", lambda: True)
    monkeypatch.setattr(service, "_find_local_partner_id", fake_find_local_partner_id)
    monkeypatch.setattr(service, "_fetch_pending_invoices", fake_fetch_pending_invoices)
    monkeypatch.setattr(service, "_find_existing_deposit", fake_find_existing_deposit)
    monkeypatch.setattr(service, "_execute", fake_execute)

    contract = {
        "id": 501,
        "code": "401748",
        "residual": 22.29,
        "partner": {"name": "Cliente prueba"},
        "partner_invoice": {"id": 654},
        "franchise": {
            "id": 77,
            "deposit": [
                {
                    "id": 11,
                    "name": "BANCO PICHINCHA",
                    "code": "PICH",
                    "number": "2100223078",
                    "is_collection": False,
                }
            ],
        },
    }
    ocr_result = {
        "texto_extraido": "Banco Pichincha deposito 7542207 valor 22,29",
        "raw": {
            "estado": "ok",
            "banco": "Banco Pichincha",
            "monto": "22,29",
            "fecha": date.today().isoformat(),
            "numero_transaccion": "7542207",
        },
    }
    attachments = [{"base64_data": "data:image/jpeg;base64,aGVsbG8="}]

    result = await service.register_payment(
        contract=contract,
        ocr_result=ocr_result,
        attachments=attachments,
        cedula="0912345678",
    )

    assert result["status"] == "created"
    assert result["deposit_id"] == 840
    assert captured["calls"][0][0] == "app.gonet.deposit"
    assert captured["calls"][0][1] == "create"
    create_values = captured["calls"][0][2][0]
    assert create_values["franchise_id"] == 77
    assert create_values["partner_id"] == 4321
    assert create_values["dni"] == "0912345678"
    assert create_values["name"] == "7542207"
    assert create_values["deposit_id"] == 11
    assert create_values["value"] == pytest.approx(22.29)
    assert create_values["pending_value"] == pytest.approx(22.29)
    assert create_values["image"] == "aGVsbG8="
    contract_values = captured["calls"][1][2][0]
    assert contract_values["name"] == "401748"
    assert contract_values["contract_id"] == "501"
    assert contract_values["deposit_id"] == 840
    assert contract_values["invoices"] == "FAC-001,FAC-002"
    assert captured["calls"][4] == ("app.gonet.deposit", "action_reconnect", ([840],))
    assert result["resolved"]["reconnect_status"] == "done"


@pytest.mark.asyncio
async def test_register_payment_enriches_contact_center_contract_and_skips_invoice_dependency(monkeypatch):
    service = BillingRegistrationService()
    captured = {"calls": [], "reconnect": None}

    async def fake_find_local_partner_id(dni: str | None):
        assert dni == "0907938674"
        return None

    async def fake_find_existing_deposit(code: str | None):
        return None

    async def fake_fetch_pending_invoices(*, franchise_id: int, partner_invoice_id: int, contract_id: int):
        raise AssertionError("contact_center fallback should not request pending invoices")

    async def fake_execute(model: str, method: str, *method_args):
        captured["calls"].append((model, method, method_args))
        if method == "create":
            return 840
        return True

    async def fake_execute_kw(model: str, method: str, *, args=None, kwargs=None):
        if model == "app.gonet.franchise" and method == "search_read":
            return [{"id": 1, "name": "MARVICNET CIA. LTDA.", "code": 5}]
        if model == "app.gonet.franchise.deposit" and method == "search_read":
            return [
                {
                    "id": 1,
                    "name": "BANCO PICHINCHA Cta. Cte. # 2100174952",
                    "code": "BPCH",
                    "number": "2100174952",
                    "is_collection": False,
                }
            ]
        raise AssertionError(f"unexpected execute_kw: {model}.{method}")

    async def fake_reconnect_with_tolerance(*, deposit_id: int, local_contract_record_id: int | None, local_contract_json: str | None, resolved: dict):
        captured["reconnect"] = {
            "deposit_id": deposit_id,
            "local_contract_record_id": local_contract_record_id,
            "local_contract_json": local_contract_json,
            "resolved": resolved,
        }
        return {"id": 220229, "model": "sale.subscription"}

    monkeypatch.setattr(service, "_is_configured", lambda: True)
    monkeypatch.setattr(service, "_find_local_partner_id", fake_find_local_partner_id)
    monkeypatch.setattr(service, "_find_existing_deposit", fake_find_existing_deposit)
    monkeypatch.setattr(service, "_fetch_pending_invoices", fake_fetch_pending_invoices)
    monkeypatch.setattr(service, "_execute", fake_execute)
    monkeypatch.setattr(service, "_execute_kw", fake_execute_kw)
    monkeypatch.setattr(service, "_reconnect_with_tolerance", fake_reconnect_with_tolerance)

    contract = {
        "id": None,
        "code": "200462",
        "source": "contact_center",
        "residual": 22.89,
        "partner": {"dni": "0907938674", "name": "FERNANDEZ TROYA MANUEL HIGINIO"},
        "partner_invoice": {"id": None},
        "franchise": {"id": None, "name": "MARVICNET CIA LTDA", "deposit": []},
    }
    ocr_result = {
        "texto_extraido": "Banco Pichincha deposito 150989107 valor 20,00",
        "raw": {
            "estado": "ok",
            "banco": "Banco Pichincha",
            "monto": "20,00",
            "fecha": date.today().isoformat(),
            "numero_transaccion": "150989107",
        },
    }
    attachments = [{"base64_data": "data:image/jpeg;base64,aGVsbG8="}]

    result = await service.register_payment(
        contract=contract,
        ocr_result=ocr_result,
        attachments=attachments,
        cedula="0907938674",
    )

    assert result["status"] == "created"
    assert result["deposit_id"] == 840
    assert result["resolved"]["limited_registration"] is True
    assert result["resolved"]["franchise_id"] == 1
    assert result["resolved"]["reconnect_status"] == "done"
    create_values = captured["calls"][0][2][0]
    assert create_values["franchise_id"] == 1
    assert create_values["contract"] == "200462"
    assert create_values["deposit_id"] == 1
    assert create_values["value"] == pytest.approx(20.00)
    contract_values = captured["calls"][1][2][0]
    assert contract_values["name"] == "200462"
    assert contract_values["contract_id"] == "200462"
    assert contract_values["invoices"] == ""
    assert captured["reconnect"] is not None
    assert captured["reconnect"]["deposit_id"] == 840
    assert captured["reconnect"]["local_contract_record_id"] == 840
    assert captured["reconnect"]["resolved"]["contract"] == "200462"
    assert captured["reconnect"]["resolved"]["pending_value"] == pytest.approx(22.89)


@pytest.mark.asyncio
async def test_register_payment_uses_direct_reconnect_for_contact_center_full_payment(monkeypatch):
    service = BillingRegistrationService()
    captured = {"calls": [], "reconnect": None}

    async def fake_find_local_partner_id(dni: str | None):
        assert dni == "1724050628"
        return None

    async def fake_find_existing_deposit(code: str | None):
        return None

    async def fake_fetch_pending_invoices(*, franchise_id: int, partner_invoice_id: int, contract_id: int):
        raise AssertionError("contact_center fallback should not request pending invoices")

    async def fake_execute(model: str, method: str, *method_args):
        captured["calls"].append((model, method, method_args))
        if model == "app.gonet.deposit" and method == "create":
            return 855
        if model == "app.gonet.contract" and method == "create":
            return 182
        return True

    async def fake_execute_kw(model: str, method: str, *, args=None, kwargs=None):
        if model == "app.gonet.franchise" and method == "search_read":
            return [{"id": 1, "name": "MARVICNET CIA. LTDA.", "code": 5}]
        if model == "app.gonet.franchise.deposit" and method == "search_read":
            return [
                {
                    "id": 1,
                    "name": "BANCO PICHINCHA Cta. Cte. # 2100174952",
                    "code": "BPCH",
                    "number": "2100174952",
                    "is_collection": False,
                }
            ]
        raise AssertionError(f"unexpected execute_kw: {model}.{method}")

    async def fake_reconnect_with_tolerance(*, deposit_id: int, local_contract_record_id: int | None, local_contract_json: str | None, resolved: dict):
        captured["reconnect"] = {
            "deposit_id": deposit_id,
            "local_contract_record_id": local_contract_record_id,
            "local_contract_json": local_contract_json,
            "resolved": resolved,
        }
        return {"id": 300856, "model": "sale.subscription"}

    monkeypatch.setattr(service, "_is_configured", lambda: True)
    monkeypatch.setattr(service, "_find_local_partner_id", fake_find_local_partner_id)
    monkeypatch.setattr(service, "_find_existing_deposit", fake_find_existing_deposit)
    monkeypatch.setattr(service, "_fetch_pending_invoices", fake_fetch_pending_invoices)
    monkeypatch.setattr(service, "_execute", fake_execute)
    monkeypatch.setattr(service, "_execute_kw", fake_execute_kw)
    monkeypatch.setattr(service, "_reconnect_with_tolerance", fake_reconnect_with_tolerance)

    result = await service.register_payment(
        contract={
            "id": None,
            "code": "300856",
            "state": "cortado",
            "residual": 22.89,
            "source": "contact_center",
            "partner": {"dni": "1724050628", "name": "SOTOMAYOR CUN PAOLA ELIZABETH"},
            "partner_invoice": {"id": None},
            "franchise": {"id": None, "name": "MARVICNET CIA LTDA", "deposit": []},
        },
        ocr_result={
            "texto_extraido": "Banco Pichincha deposito 225254 valor 22,89",
            "raw": {
                "estado": "ok",
                "banco": "Banco Pichincha",
                "monto": "22,89",
                "fecha": date.today().isoformat(),
                "numero_transaccion": "225254",
            },
        },
        attachments=[{"base64_data": "data:image/jpeg;base64,aGVsbG8="}],
        cedula="1724050628",
    )

    assert result["status"] == "created"
    assert result["deposit_id"] == 855
    assert result["resolved"]["limited_registration"] is True
    assert result["resolved"]["reconnect_status"] == "done"
    assert result["resolved"]["remote_contract_id"] == 300856
    assert captured["reconnect"] is not None
    assert captured["reconnect"]["deposit_id"] == 855
    assert captured["reconnect"]["local_contract_record_id"] == 182
    assert all(call[:2] != ("app.gonet.deposit", "action_reconnect") for call in captured["calls"])


@pytest.mark.asyncio
async def test_register_payment_returns_error_when_odoo_create_fails(monkeypatch):
    service = BillingRegistrationService()

    async def fake_find_local_partner_id(dni: str | None):
        assert dni == "0912345678"
        return 4321

    async def fake_fetch_pending_invoices(*, franchise_id: int, partner_invoice_id: int, contract_id: int):
        return [{"id": 9001, "number": "FAC-001", "residual": 22.29}]

    async def fake_find_existing_deposit(code: str | None):
        return None

    async def fake_execute(model: str, method: str, *method_args):
        if model == "app.gonet.deposit" and method == "create":
            raise RuntimeError("odoo unavailable")
        raise AssertionError(f"unexpected execute call: {model}.{method}")

    monkeypatch.setattr(service, "_is_configured", lambda: True)
    monkeypatch.setattr(service, "_find_local_partner_id", fake_find_local_partner_id)
    monkeypatch.setattr(service, "_fetch_pending_invoices", fake_fetch_pending_invoices)
    monkeypatch.setattr(service, "_find_existing_deposit", fake_find_existing_deposit)
    monkeypatch.setattr(service, "_execute", fake_execute)

    result = await service.register_payment(
        contract={
            "id": 501,
            "code": "401748",
            "residual": 22.29,
            "partner": {"name": "Cliente prueba"},
            "partner_invoice": {"id": 654},
            "franchise": {
                "id": 77,
                "deposit": [
                    {
                        "id": 11,
                        "name": "BANCO PICHINCHA",
                        "code": "PICH",
                        "number": "2100223078",
                        "is_collection": False,
                    }
                ],
            },
        },
        ocr_result={
            "texto_extraido": "Banco Pichincha deposito 7542207 valor 22,29",
            "raw": {
                "estado": "ok",
                "banco": "Banco Pichincha",
                "monto": "22,29",
                "numero_transaccion": "7542207",
            },
        },
        attachments=[{"base64_data": "data:image/jpeg;base64,aGVsbG8="}],
        cedula="0912345678",
    )

    assert result["status"] == "error"
    assert result["resolved"]["code"] == "7542207"
