"""Carga rápida contra la API para medir el comportamiento básico del servicio."""

import argparse
import asyncio
import time

import httpx


async def main(base_url: str, requests_count: int) -> None:
    """Punto de entrada del módulo."""
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=10.0) as client:
        responses = await asyncio.gather(*[client.get(f"{base_url.rstrip('/')}/health") for _ in range(requests_count)])
    elapsed = time.perf_counter() - started
    ok = sum(1 for response in responses if response.status_code == 200)
    print({"requests": requests_count, "ok": ok, "seconds": round(elapsed, 3)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--requests", type=int, default=50)
    args = parser.parse_args()
    asyncio.run(main(args.base_url, args.requests))

