import argparse
import asyncio
import json
import math
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * percent
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return values[lower]
    weight = rank - lower
    return values[lower] * (1 - weight) + values[upper] * weight


async def _worker(
    *,
    client: httpx.AsyncClient,
    url: str,
    payload: tuple[str, bytes, str],
    pending: asyncio.Queue[int],
    latencies_ms: list[float],
    status_counts: Counter[int],
    errors: Counter[str],
) -> None:
    while True:
        try:
            pending.get_nowait()
        except asyncio.QueueEmpty:
            return

        started = time.perf_counter()
        try:
            response = await client.post(url, files={"file": payload})
            status_counts[int(response.status_code)] += 1
        except Exception as exc:
            errors[type(exc).__name__] += 1
        finally:
            latencies_ms.append((time.perf_counter() - started) * 1000.0)
            pending.task_done()


async def run_load_test(args: argparse.Namespace) -> dict[str, Any]:
    file_path = Path(args.file).resolve()
    file_bytes = file_path.read_bytes()
    payload = (file_path.name, file_bytes, args.content_type)

    queue: asyncio.Queue[int] = asyncio.Queue()
    for index in range(args.requests):
        queue.put_nowait(index)

    status_counts: Counter[int] = Counter()
    errors: Counter[str] = Counter()
    latencies_ms: list[float] = []

    limits = httpx.Limits(
        max_connections=max(args.concurrency * 2, 20),
        max_keepalive_connections=max(args.concurrency, 20),
    )
    timeout = httpx.Timeout(args.timeout)

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        workers = [
            asyncio.create_task(
                _worker(
                    client=client,
                    url=args.url,
                    payload=payload,
                    pending=queue,
                    latencies_ms=latencies_ms,
                    status_counts=status_counts,
                    errors=errors,
                )
            )
            for _ in range(args.concurrency)
        ]
        await asyncio.gather(*workers)
    elapsed = time.perf_counter() - started

    latencies_ms.sort()
    completed = sum(status_counts.values()) + sum(errors.values())
    success = sum(count for status, count in status_counts.items() if 200 <= status < 300)

    return {
        "url": args.url,
        "file": str(file_path),
        "requests": args.requests,
        "concurrency": args.concurrency,
        "completed": completed,
        "success": success,
        "errors": dict(errors),
        "status_counts": dict(sorted(status_counts.items())),
        "elapsed_seconds": round(elapsed, 3),
        "throughput_rps": round((completed / elapsed) if elapsed > 0 else 0.0, 2),
        "latency_ms": {
            "avg": round(statistics.fmean(latencies_ms), 2) if latencies_ms else 0.0,
            "min": round(min(latencies_ms), 2) if latencies_ms else 0.0,
            "p50": round(_percentile(latencies_ms, 0.50), 2),
            "p95": round(_percentile(latencies_ms, 0.95), 2),
            "p99": round(_percentile(latencies_ms, 0.99), 2),
            "max": round(max(latencies_ms), 2) if latencies_ms else 0.0,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Async load test runner for the OCR API.")
    parser.add_argument("--url", default="http://127.0.0.1:8000/v1/ocr")
    parser.add_argument("--file", required=True)
    parser.add_argument("--content-type", default="image/jpeg")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()

    report = asyncio.run(run_load_test(args))
    if args.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return

    print(f"url={report['url']}")
    print(f"file={report['file']}")
    print(
        f"requests={report['requests']} concurrency={report['concurrency']} completed={report['completed']} success={report['success']}"
    )
    print(f"elapsed_seconds={report['elapsed_seconds']} throughput_rps={report['throughput_rps']}")
    print(f"status_counts={report['status_counts']} errors={report['errors']}")
    print(f"latency_ms={report['latency_ms']}")


if __name__ == "__main__":
    main()
