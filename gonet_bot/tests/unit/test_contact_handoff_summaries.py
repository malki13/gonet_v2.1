from packages.agents.contact_billing_utils import (
    format_billing_async_retry_message,
    format_billing_handoff_summary,
    format_billing_proof_nudge,
    format_billing_proof_request,
)
from packages.agents.contact_support_utils import format_support_handoff_summary


def test_billing_proof_request_and_nudge_require_document_date_and_amount():
    request_message = format_billing_proof_request().lower()
    nudge_message = format_billing_proof_nudge().lower()

    for message in (request_message, nudge_message):
        assert "clara" in message
        assert "numero del documento" in message or "número del documento" in message
        assert "fecha" in message
        assert "monto" in message


def test_billing_async_retry_message_mentions_document_date_and_amount():
    message = format_billing_async_retry_message(kind="retry_image", attempt=1).lower()

    assert "numero del documento" in message or "número del documento" in message
    assert "fecha" in message
    assert "monto" in message
    assert "foto clara" in message


def test_billing_handoff_summary_is_structured_for_odoo():
    summary = format_billing_handoff_summary(
        reason="No se pudo registrar el pago automáticamente",
        contract={"code": "800003", "state": "cortado", "residual": "116.16"},
        registration={
            "status": "missing_fields",
            "missing": ["value"],
            "resolved": {
                "code": "61146151",
                "value": 18.5,
                "ocr_date": "2026-04-14",
                "pending_value": 116.16,
                "deposit": {"name": "BANCO PICHINCHA"},
                "date_diff_days": 3,
                "max_ocr_date_diff_days": 15,
            },
        },
        ocr_result={
            "status": "validated",
            "estado": "validated",
            "texto_extraido": "Comprobante 61146151 valor 18.50 fecha 2026-04-14",
        },
        proof_attempts=2,
        proof_failures=["Intento 1: OCR pidió reenviar la imagen por baja legibilidad"],
    )

    assert "Caso: Facturación" in summary
    assert "Subcaso: Validación de comprobante" in summary
    assert "Contrato:" in summary
    assert "OCR:" in summary
    assert "- Número detectado: 61146151" in summary
    assert "- Fecha detectada: 2026-04-14" in summary
    assert "- Monto detectado: 18.50" in summary
    assert "Registro automático:" in summary
    assert "- Campos faltantes: value" in summary


def test_support_handoff_summary_includes_diagnostic_context_and_observations():
    summary = format_support_handoff_summary(
        reason="La lentitud persiste después de pruebas guiadas.",
        contract={
            "code": "500007",
            "state": "active",
            "status_label": "activo",
            "partner_name": "Christian Montero",
        },
        issue_type="slow_internet",
        diagnostic_context={
            "onu_status": "working",
            "power_dbm": -20.13,
            "connected_devices": 12,
            "plan_name": "PLAN GOPLUS",
            "plan_speed_mbps": 350,
            "device_model": "HUAWEI AX3",
            "network_count": 2,
            "lan_devices": 4,
            "mesh_devices": 1,
            "wifi_devices": 7,
            "wifi_24g_devices": 3,
            "wifi_5g_devices": 4,
            "rebooted": True,
            "hypothesis": "wifi_load",
            "next_step": "handoff_specialist",
        },
        observations={
            "device_scope": "all_devices",
            "connection_type": "wifi",
            "near_router_result": "same",
            "affected_service": "streaming",
        },
        system_detail="Se realizaron validaciones automáticas y la lentitud continúa.",
    )

    assert "Caso: Servicio técnico" in summary
    assert "Cliente / contrato:" in summary
    assert "- Contrato: 500007" in summary
    assert "- Tipo de inconveniente: internet lento" in summary
    assert "Diagnóstico disponible:" in summary
    assert "- Estado ONU: operativa" in summary
    assert "- Velocidad reportada por CPE: 350 Mbps" in summary
    assert "- Acción remota ejecutada: reinicio remoto de ONU y router" in summary
    assert "wifi_load" not in summary
    assert "handoff_specialist" not in summary
    assert "Hipótesis actual:" in summary
    assert "Siguiente paso sugerido:" in summary
    assert "Observaciones del cliente:" in summary
    assert "- Medio reportado: solo wifi" in summary
    assert "Detalle técnico previo:" in summary
