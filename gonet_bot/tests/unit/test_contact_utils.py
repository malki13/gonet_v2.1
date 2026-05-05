from packages.agents.contact_utils import (
    format_contract_holder_identity_request,
    format_contract_selection,
    format_billing_duplicate_message,
    format_billing_options,
    format_information_consent_prompt,
    format_support_clarification,
    format_support_issue_triage_reply,
    format_support_monitoring_reply,
    match_contract_in_text,
    normalize_billing_action,
    normalize_contract_rows,
    user_requests_human,
)


def test_normalize_contract_rows_ignores_invalid_json_strings():
    rows = normalize_contract_rows(
        [
            {"response": '[{"code": "CT-001"}]'},
            "not-a-json-payload",
            {"code": "CT-002"},
        ]
    )

    assert rows == [{"code": "CT-001"}, {"code": "CT-002"}]


def test_match_contract_in_text_supports_contract_index_phrases():
    contracts = [{"code": "701177"}, {"code": "800003"}]

    assert match_contract_in_text("el contrato 2", contracts) == "800003"
    assert match_contract_in_text("segunda opción", contracts) == "800003"


def test_match_contract_in_text_supports_human_contract_references():
    contracts = [
        {"code": "701177", "state": "active", "residual": "0.00"},
        {"code": "800003", "state": "cortado", "residual": "116.16"},
    ]

    assert match_contract_in_text("el cortado", contracts) == "800003"
    assert match_contract_in_text("el que debe", contracts) == "800003"
    assert match_contract_in_text("el activo", contracts) == "701177"
    assert match_contract_in_text("el de 116.16", contracts) == "800003"


def test_normalize_billing_action_supports_natural_option_references():
    assert normalize_billing_action("la primera") == "Registrar Pago"
    assert normalize_billing_action("te paso el recibo entonces") == "Registrar Pago"
    assert normalize_billing_action("el link") == "Link de Cobro"
    assert normalize_billing_action("mándame el enlace") == "Link de Cobro"


def test_user_requests_human_accepts_bare_asesor():
    assert user_requests_human("asesor") is True
    assert user_requests_human("hola un asesor por favor") is True
    assert user_requests_human("buenas necesito un asesor") is True


def test_contact_copy_uses_more_human_identity_and_consent_prompts():
    identity = format_contract_holder_identity_request()
    missing = format_contract_holder_identity_request(contract_not_found=True)
    consent = format_information_consent_prompt({"partner_name": "Nancy Mercedes"})

    assert identity == "Compártame la cédula o RUC del titular del contrato y lo reviso."
    assert "compartame la cedula o ruc del titular" in missing.lower() or "compártame la cédula o ruc del titular" in missing.lower()
    assert "me confirma" in consent.lower()
    assert "continuar por aquí" in consent.lower() or "continuar por aqui" in consent.lower()


def test_contact_copy_uses_more_human_contract_selection_and_support_prompts():
    contracts = [
        {"code": "701177", "state": "cortado", "partner_name": "Nancy Mercedes", "residual": "164.94"},
        {"code": "800003", "state": "activo", "partner_name": "Nancy Mercedes", "residual": "0.00"},
    ]
    selection = format_contract_selection(contracts)
    clarify = format_support_clarification({"code": "500007"})
    triage = format_support_issue_triage_reply({"code": "500007"})
    monitoring = format_support_monitoring_reply("no_service", {"code": "500007"})

    assert "para no equivocarme" in selection.lower()
    assert "respóndame con el número" in selection.lower() or "respondame con el numero" in selection.lower()
    assert "qué está ocurriendo" in clarify.lower() or "que esta ocurriendo" in clarify.lower()
    assert "prefiere que lo derive con un asesor especializado" in clarify.lower()
    assert "hagamos una prueba rápida" in triage.lower() or "hagamos una prueba rapida" in triage.lower()
    assert "revise una sola vez" in monitoring.lower()
    assert "lo derivo con un asesor especializado" in monitoring.lower()


def test_contact_copy_uses_clear_billing_duplicate_and_pending_payment_copy():
    options = format_billing_options(
        {
            "code": "800003",
            "state": "cortado",
            "partner_name": "Nancy Mercedes",
            "residual": "116.16",
        },
        "0102030405",
    )
    duplicate = format_billing_duplicate_message()

    lowered = options.lower()
    assert "registra un pago pendiente" in lowered
    assert "ya revisé su contrato" in lowered
    assert "ya está registrado" in duplicate.lower() or "ya esta registrado" in duplicate.lower()
    assert "no es válido" in duplicate.lower() or "no es valido" in duplicate.lower()
