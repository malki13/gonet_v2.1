"""Persistencia en Postgres de códigos OTP y su estado."""

import logging

from packages.integrations.runtime import get_postgres_pool

logger = logging.getLogger("postgres_otp")


async def insert_otp(recipient: str, otp: str) -> None:
    """Devuelve el otp insert."""
    pool = await get_postgres_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO otp_bot (recipient, otp, fecha) VALUES ($1, $2, NOW())",
            recipient,
            otp,
        )


async def get_last_otp(recipient: str):
    """Devuelve last otp."""
    pool = await get_postgres_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT otp, fecha
            FROM otp_bot
            WHERE recipient = $1
            ORDER BY fecha DESC
            LIMIT 1
            """,
            recipient,
        )
        if not row:
            return None
        return {"otp": row["otp"], "fecha": row["fecha"]}

