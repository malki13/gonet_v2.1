from fastapi.testclient import TestClient


def _accept_consent(client: TestClient, *, session_id: str, recipient: str = "593999", channel: str = "whatsapp"):
    return client.post(
        "/v1/messages",
        json={
            "mensaje": "ACEPTO",
            "channel": channel,
            "recipient": recipient,
            "session_id": session_id,
            "metadata": {"interactive_reply_id": "ASISTENCIA_ACEPTO"},
        },
    )
