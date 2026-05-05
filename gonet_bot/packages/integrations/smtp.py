"""Cliente SMTP para notificaciones operativas."""

import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr

from packages.shared.config import get_settings

logger = logging.getLogger("smtp")


class SMTPClient:
    """Cliente de Cliente SMTP para envios de correo.."""
    def __init__(self) -> None:
        """Inicializa el smtpclient con la configuracion necesaria."""
        self.settings = get_settings()

    def _send_sync(self, to_email: str, subject: str, body: str) -> None:
        """Envía sync."""
        name, addr = parseaddr(self.settings.smtp_from)
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = formataddr((name or "GoNet", addr or self.settings.smtp_user or ""))
        msg["To"] = to_email

        if int(self.settings.smtp_port) == 465:
            with smtplib.SMTP_SSL(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as server:
                server.login(self.settings.smtp_user, self.settings.smtp_pass)
                server.sendmail(msg["From"], [to_email], msg.as_string())
        else:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as server:
                server.starttls()
                server.login(self.settings.smtp_user, self.settings.smtp_pass)
                server.sendmail(msg["From"], [to_email], msg.as_string())

    async def send_email(self, to_email: str, subject: str, body: str) -> dict:
        """Envía email."""
        if not all([self.settings.smtp_host, self.settings.smtp_user, self.settings.smtp_pass]):
            return {"status": "skipped", "reason": "smtp_not_configured"}
        await asyncio.to_thread(self._send_sync, to_email, subject, body)
        return {"status": "sent", "recipient": to_email}
