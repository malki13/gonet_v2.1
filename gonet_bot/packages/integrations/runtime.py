"""Recursos compartidos de runtime para HTTP, Postgres y Redis."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

from packages.shared.config import get_settings

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None

if TYPE_CHECKING:
    import asyncpg as asyncpg_module

    AsyncPGPool = asyncpg_module.Pool
else:  # pragma: no cover
    AsyncPGPool = Any

logger = logging.getLogger("runtime")

_http_client: httpx.AsyncClient | None = None
_http_client_lock = asyncio.Lock()

_postgres_pool: AsyncPGPool | None = None
_postgres_pool_lock = asyncio.Lock()

_redis_client = None
_redis_client_lock = asyncio.Lock()


async def get_shared_http_client() -> httpx.AsyncClient:
    """Devuelve cliente de shared http."""
    global _http_client
    if _http_client is not None:
        return _http_client
    async with _http_client_lock:
        if _http_client is None:
            _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


async def get_postgres_pool() -> AsyncPGPool:
    """Devuelve postgres pool."""
    global _postgres_pool
    settings = get_settings()
    dsn = settings.otp_pg_dsn or settings.pg_dsn
    if not dsn:
        raise ValueError("OTP_PG_DSN/PG_DSN no configurado")
    if asyncpg is None:
        raise RuntimeError("asyncpg_unavailable")
    if _postgres_pool is not None:
        return _postgres_pool
    async with _postgres_pool_lock:
        if _postgres_pool is None:
            _postgres_pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10, command_timeout=10)
    return _postgres_pool


async def get_redis_client():
    """Devuelve cliente de redis."""
    global _redis_client
    settings = get_settings()
    if not settings.redis_url:
        raise ValueError("REDIS_URL no configurado")
    if _redis_client is not None:
        return _redis_client
    async with _redis_client_lock:
        if _redis_client is None:
            from redis.asyncio import Redis

            _redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client
