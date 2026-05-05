import argparse
import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import psutil

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_test import run_load_test


def _parse_levels(raw: str) -> list[int]:
    levels: list[int] = []
    for token in raw.split(","):
        value = int(token.strip())
        if value <= 0:
            raise ValueError("All levels must be positive integers")
        if value not in levels:
            levels.append(value)
    if not levels:
        raise ValueError("At least one level is required")
    return levels


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _endpoint_path(mode: str) -> str:
    if mode == "tesseract":
        return "/v1/ocr-tesseract"
    return "/v1/ocr"


def _build_load_test_args(
    *,
    url: str,
    file_path: Path,
    content_type: str,
    requests: int,
    concurrency: int,
    timeout: float,
) -> argparse.Namespace:
    return argparse.Namespace(
        url=url,
        file=str(file_path),
        content_type=content_type,
        requests=requests,
        concurrency=concurrency,
        timeout=timeout,
        json_output=False,
    )


class ProcessTreeMonitor:
    def __init__(self, root_pid: int, interval_seconds: float) -> None:
        self.root_pid = root_pid
        self.interval_seconds = max(0.1, interval_seconds)
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._primed_pids: set[int] = set()
        self._thread = threading.Thread(target=self._loop, name=f"monitor-{root_pid}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(2.0, self.interval_seconds * 4))

    def mark(self) -> int:
        return len(self.samples)

    def summarize(self, start_index: int) -> dict[str, Any]:
        selected = self.samples[start_index:]
        if not selected:
            current = self._sample()
            if current["process_count"] > 0 or current["rss_bytes"] > 0:
                selected = [current]
        if not selected:
            return {
                "samples": 0,
                "cpu_percent": {"avg": 0.0, "max": 0.0},
                "rss_mb": {"avg": 0.0, "max": 0.0},
                "process_count": {"avg": 0.0, "max": 0},
            }

        cpu_values = [sample["cpu_percent"] for sample in selected]
        rss_values = [sample["rss_bytes"] / (1024.0 * 1024.0) for sample in selected]
        process_counts = [sample["process_count"] for sample in selected]
        return {
            "samples": len(selected),
            "cpu_percent": {
                "avg": round(statistics.fmean(cpu_values), 2),
                "max": round(max(cpu_values), 2),
            },
            "rss_mb": {
                "avg": round(statistics.fmean(rss_values), 2),
                "max": round(max(rss_values), 2),
            },
            "process_count": {
                "avg": round(statistics.fmean(process_counts), 2),
                "max": int(max(process_counts)),
            },
        }

    def _loop(self) -> None:
        self._prime_existing_processes()
        while not self._stop.wait(self.interval_seconds):
            self.samples.append(self._sample())

    def _prime_existing_processes(self) -> None:
        for process in self._process_tree():
            self._prime(process)

    def _sample(self) -> dict[str, Any]:
        rss_bytes = 0
        cpu_percent = 0.0
        process_count = 0
        pids: list[int] = []

        for process in self._process_tree():
            self._prime(process)
            try:
                rss_bytes += int(process.memory_info().rss)
                cpu_percent += float(process.cpu_percent(interval=None))
                process_count += 1
                pids.append(int(process.pid))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        return {
            "timestamp": round(time.time(), 3),
            "rss_bytes": rss_bytes,
            "cpu_percent": round(cpu_percent, 2),
            "process_count": process_count,
            "pids": sorted(pids),
        }

    def _process_tree(self) -> list[psutil.Process]:
        try:
            root = psutil.Process(self.root_pid)
        except psutil.NoSuchProcess:
            return []

        try:
            processes = [root, *root.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return []

        alive: list[psutil.Process] = []
        for process in processes:
            try:
                if process.is_running():
                    alive.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return alive

    def _prime(self, process: psutil.Process) -> None:
        if process.pid in self._primed_pids:
            return
        try:
            process.cpu_percent(interval=None)
            self._primed_pids.add(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return


def _wait_for_server(url: str, timeout_seconds: float) -> None:
    deadline = time.perf_counter() + max(1.0, timeout_seconds)
    while time.perf_counter() < deadline:
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code >= 100:
                return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"Server did not become reachable within {timeout_seconds} seconds: {url}")


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return

    try:
        root = psutil.Process(process.pid)
        descendants = root.children(recursive=True)
    except psutil.NoSuchProcess:
        return

    for child in descendants:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            continue

    try:
        root.terminate()
    except psutil.NoSuchProcess:
        return

    gone, alive = psutil.wait_procs([*descendants, root], timeout=5.0)
    if gone:
        process.wait(timeout=5.0)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            continue
    try:
        process.wait(timeout=5.0)
    except Exception:
        pass


def _start_server(
    *,
    project_root: Path,
    logs_dir: Path,
    args: argparse.Namespace,
    ocr_max_concurrency: int,
) -> tuple[subprocess.Popen[Any], Path, str]:
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    endpoint_url = f"{base_url}{_endpoint_path(args.mode)}"
    log_path = logs_dir / f"{args.mode}-ocrmax-{ocr_max_concurrency}.log"

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["OCR_MAX_CONCURRENCY"] = str(ocr_max_concurrency)
    env["OCR_QUEUE_TIMEOUT_SECONDS"] = str(args.queue_timeout_seconds)
    env["OCR_REQUEST_TIMEOUT_SECONDS"] = str(args.request_timeout_seconds)
    env["NOTIFY_MAX_CONCURRENCY"] = str(args.notify_max_concurrency)
    if args.mode == "tesseract" and not env.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = "benchmark-placeholder-key"

    command = [
        sys.executable,
        "-m",
        "hypercorn",
        "app:app",
        "--bind",
        f"127.0.0.1:{port}",
        "--workers",
        str(args.web_concurrency),
    ]

    log_file = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=project_root,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_server(endpoint_url, args.startup_timeout_seconds)
    except Exception:
        log_file.close()
        _terminate_process_tree(process)
        raise
    return process, log_path, endpoint_url


async def _warmup(endpoint_url: str, file_path: Path, content_type: str, timeout: float, requests: int) -> None:
    if requests <= 0:
        return
    args = _build_load_test_args(
        url=endpoint_url,
        file_path=file_path,
        content_type=content_type,
        requests=requests,
        concurrency=1,
        timeout=timeout,
    )
    await run_load_test(args)


def _success_rate(report: dict[str, Any]) -> float:
    completed = int(report.get("completed", 0) or 0)
    if completed <= 0:
        return 0.0
    success = int(report.get("success", 0) or 0)
    return round(success / completed, 4)


def _qualified(report: dict[str, Any], resources: dict[str, Any], args: argparse.Namespace) -> bool:
    success_rate = _success_rate(report)
    p95 = float(report.get("latency_ms", {}).get("p95", 0.0) or 0.0)
    rss_max = float(resources.get("rss_mb", {}).get("max", 0.0) or 0.0)
    return (
        success_rate >= args.min_success_rate
        and p95 <= args.max_p95_ms
        and rss_max <= args.max_rss_mb
    )


async def _run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    project_root = PROJECT_ROOT
    file_path = Path(args.file).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if args.mode == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY must be set for mode=openai")

    logs_dir = Path(args.logs_dir).resolve()
    logs_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": args.mode,
        "file": str(file_path),
        "content_type": args.content_type,
        "requests_per_run": args.requests,
        "client_concurrency_levels": args.client_concurrency_levels,
        "ocr_max_concurrency_levels": args.ocr_max_concurrency_levels,
        "web_concurrency": args.web_concurrency,
        "queue_timeout_seconds": args.queue_timeout_seconds,
        "request_timeout_seconds": args.request_timeout_seconds,
        "qualification_thresholds": {
            "min_success_rate": args.min_success_rate,
            "max_p95_ms": args.max_p95_ms,
            "max_rss_mb": args.max_rss_mb,
        },
        "runs": [],
    }

    for ocr_max_concurrency in args.ocr_max_concurrency_levels:
        process, log_path, endpoint_url = _start_server(
            project_root=project_root,
            logs_dir=logs_dir,
            args=args,
            ocr_max_concurrency=ocr_max_concurrency,
        )
        monitor = ProcessTreeMonitor(process.pid, args.sample_interval_seconds)
        monitor.start()
        try:
            await _warmup(
                endpoint_url,
                file_path,
                args.content_type,
                args.timeout,
                args.warmup_requests,
            )
            for client_concurrency in args.client_concurrency_levels:
                marker = monitor.mark()
                load_args = _build_load_test_args(
                    url=endpoint_url,
                    file_path=file_path,
                    content_type=args.content_type,
                    requests=args.requests,
                    concurrency=client_concurrency,
                    timeout=args.timeout,
                )
                load_report = await run_load_test(load_args)
                resources = monitor.summarize(marker)
                success_rate = _success_rate(load_report)
                report["runs"].append(
                    {
                        "mode": args.mode,
                        "ocr_max_concurrency": ocr_max_concurrency,
                        "client_concurrency": client_concurrency,
                        "endpoint_url": endpoint_url,
                        "server_log": str(log_path),
                        "load": load_report,
                        "resources": resources,
                        "success_rate": success_rate,
                        "qualified": _qualified(load_report, resources, args),
                    }
                )
        finally:
            monitor.stop()
            _terminate_process_tree(process)

    recommendations: list[dict[str, Any]] = []
    for ocr_max_concurrency in args.ocr_max_concurrency_levels:
        rows = [row for row in report["runs"] if row["ocr_max_concurrency"] == ocr_max_concurrency]
        qualified_rows = [row for row in rows if row["qualified"]]
        best_row = max(qualified_rows, key=lambda row: row["client_concurrency"], default=None)
        recommendations.append(
            {
                "ocr_max_concurrency": ocr_max_concurrency,
                "max_qualified_client_concurrency": (
                    best_row["client_concurrency"] if best_row is not None else 0
                ),
                "selected_run": best_row,
            }
        )
    report["recommendations"] = recommendations
    return report


def _print_summary(report: dict[str, Any]) -> None:
    print(
        "ocr_max_concurrency client_concurrency success_rate p95_ms rss_max_mb cpu_avg qualified"
    )
    for row in report["runs"]:
        print(
            f"{row['ocr_max_concurrency']:>19} "
            f"{row['client_concurrency']:>18} "
            f"{row['success_rate']:>12.2%} "
            f"{row['load']['latency_ms']['p95']:>6} "
            f"{row['resources']['rss_mb']['max']:>10} "
            f"{row['resources']['cpu_percent']['avg']:>7} "
            f"{str(row['qualified']).lower():>9}"
        )

    print("")
    print("recommended_max_client_concurrency_by_ocr_max")
    for row in report.get("recommendations", []):
        print(
            f"ocr_max_concurrency={row['ocr_max_concurrency']} "
            f"max_qualified_client_concurrency={row['max_qualified_client_concurrency']}"
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark OCR concurrency matrix with CPU and memory sampling.")
    parser.add_argument("--mode", choices=["openai", "tesseract"], default="openai")
    parser.add_argument("--file", required=True)
    parser.add_argument("--content-type", default="image/jpeg")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--client-concurrency-levels", default="1,2,4,8")
    parser.add_argument("--ocr-max-concurrency-levels", default="1,2,4,8")
    parser.add_argument("--web-concurrency", type=int, default=1)
    parser.add_argument("--queue-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--notify-max-concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--warmup-requests", type=int, default=1)
    parser.add_argument("--sample-interval-seconds", type=float, default=0.5)
    parser.add_argument("--startup-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--min-success-rate", type=float, default=0.99)
    parser.add_argument("--max-p95-ms", type=float, default=5000.0)
    parser.add_argument("--max-rss-mb", type=float, default=4096.0)
    parser.add_argument("--logs-dir", default="artifacts/benchmark-logs")
    parser.add_argument("--output", default="")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    args.client_concurrency_levels = _parse_levels(args.client_concurrency_levels)
    args.ocr_max_concurrency_levels = _parse_levels(args.ocr_max_concurrency_levels)

    report = asyncio.run(_run_matrix(args))
    _print_summary(report)

    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("")
        print(f"report_written={output_path}")


if __name__ == "__main__":
    main()
