from packages.integrations.odoo_crm import OdooCRMClient


def test_odoo_crm_build_payload_includes_coordinates():
    client = OdooCRMClient()

    payload = client._build_payload(
        {
            "type": "lead",
            "partner_name": "Freddy Cabrera",
            "city": "Loja",
            "street": "Calle Ejemplo, Loja, Ecuador",
            "phone": "593961588185",
            "latitude": -4.003688,
            "longitude": -79.202511,
        }
    )

    assert payload["street"] == "Calle Ejemplo, Loja, Ecuador"
    assert payload["latitude"] == -4.003688
    assert payload["longitude"] == -79.202511
