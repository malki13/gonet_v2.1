"""Pruebas rápidas de extremo a extremo para validar los flujos principales."""

import argparse
import asyncio
import json

import httpx

async def _post(client: httpx.AsyncClient, base_url: str, payload: dict) -> dict:
    """Envía una petición `POST` HTTP y devuelve la respuesta cruda."""
    response = await client.post(f"{base_url.rstrip('/')}/v1/messages", json=payload)
    response.raise_for_status()
    return response.json()


async def run(base_url: str) -> dict:
    """Ejecuta la bateria minima de smoke tests contra la API."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        health = await client.get(f"{base_url.rstrip('/')}/health")
        health.raise_for_status()

        billing_session = "smoke-billing-cli"
        billing = [
            {"mensaje": "Necesito facturación", "channel": "whatsapp", "recipient": "593999", "session_id": billing_session, "cedula": "0102030405"},
            {"mensaje": "2", "channel": "whatsapp", "recipient": "593999", "session_id": billing_session, "cedula": "0102030405"},
            {"mensaje": "Registrar Pago", "channel": "whatsapp", "recipient": "593999", "session_id": billing_session, "cedula": "0102030405"},
            {
                "mensaje": "Adjunto comprobante",
                "channel": "whatsapp",
                "recipient": "593999",
                "session_id": billing_session,
                "cedula": "0102030405",
                "attachments": [{"filename": "proof.png", "mime_type": "image/png", "base64_data": "abc123"}],
            },
        ]

        billing_steps = [await _post(client, base_url, payload) for payload in billing]
        billing_outbox = {
            "status": "skipped",
            "reason": "direct_channel_delivery",
        }

        support_session = "smoke-support-cli"
        support = [
            {"mensaje": "No tengo internet", "channel": "whatsapp", "recipient": "593888", "session_id": support_session, "cedula": "0102030405"},
            {"mensaje": "1", "channel": "whatsapp", "recipient": "593888", "session_id": support_session, "cedula": "0102030405"},
            {"mensaje": "Sí, ya funciona", "channel": "whatsapp", "recipient": "593888", "session_id": support_session, "cedula": "0102030405"},
        ]
        support_steps = [await _post(client, base_url, payload) for payload in support]

        sales_session = "smoke-sales-cli"
        sales = [
            {"mensaje": "Quiero contratar internet para mi casa", "channel": "whatsapp", "recipient": "593777", "session_id": sales_session},
            {"mensaje": "Juan Perez", "channel": "whatsapp", "recipient": "593777", "session_id": sales_session},
            {"mensaje": "Av. Demo 123 y Primera", "channel": "whatsapp", "recipient": "593777", "session_id": sales_session},
            {"mensaje": "0999999999", "channel": "whatsapp", "recipient": "593777", "session_id": sales_session},
            {"mensaje": "-2.170998,-79.922359", "channel": "whatsapp", "recipient": "593777", "session_id": sales_session},
        ]
        sales_steps = [await _post(client, base_url, payload) for payload in sales]

        return {
            "health": health.json(),
            "billing": {"steps": billing_steps, "outbox": billing_outbox},
            "support": {"steps": support_steps},
            "sales": {"steps": sales_steps},
        }


async def main() -> None:
    """Punto de entrada del módulo."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    args = parser.parse_args()
    result = await run(args.base_url)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
