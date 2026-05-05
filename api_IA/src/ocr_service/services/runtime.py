import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, TypeVar

from ..domain.models import APIError

T = TypeVar("T")


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        return default
    return max(1, value)


def _env_float(name: str, default: float, minimum: float = 0.1) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except Exception:
        return default
    return max(minimum, value)


def _default_ocr_concurrency() -> int:
    cpu_count = os.cpu_count() or 4
    return max(2, min(8, cpu_count))


def _default_notify_concurrency() -> int:
    cpu_count = os.cpu_count() or 4
    return max(2, min(16, cpu_count * 2))


@dataclass(frozen=True)
class RuntimeConfig:
    ocr_max_concurrency: int
    ocr_queue_timeout_seconds: float
    ocr_request_timeout_seconds: float
    notify_max_concurrency: int
    notify_queue_timeout_seconds: float
    notify_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        return cls(
            ocr_max_concurrency=_env_int("OCR_MAX_CONCURRENCY", _default_ocr_concurrency()),
            ocr_queue_timeout_seconds=_env_float("OCR_QUEUE_TIMEOUT_SECONDS", 2.0),
            ocr_request_timeout_seconds=_env_float("OCR_REQUEST_TIMEOUT_SECONDS", 300.0),
            notify_max_concurrency=_env_int("NOTIFY_MAX_CONCURRENCY", _default_notify_concurrency()),
            notify_queue_timeout_seconds=_env_float("NOTIFY_QUEUE_TIMEOUT_SECONDS", 1.0),
            notify_timeout_seconds=_env_float("NOTIFY_TIMEOUT_SECONDS", 15.0),
        )


class BoundedExecutor:
    def __init__(
        self,
        *,
        name: str,
        max_concurrency: int,
        queue_timeout_seconds: float,
        default_timeout_seconds: float,
        busy_message: str,
        busy_code: str,
        busy_status: int,
        timeout_message: str,
        timeout_code: str,
        timeout_status: int,
    ) -> None:
        self.name = name
        self.max_concurrency = max(1, max_concurrency)
        self.queue_timeout_seconds = max(0.1, queue_timeout_seconds)
        self.default_timeout_seconds = max(0.1, default_timeout_seconds)
        self.busy_message = busy_message
        self.busy_code = busy_code
        self.busy_status = busy_status
        self.timeout_message = timeout_message
        self.timeout_code = timeout_code
        self.timeout_status = timeout_status
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_concurrency,
            thread_name_prefix=f"ocr-{name}",
        )
        self._in_flight = 0
        self._waiting = 0

    async def run(
        self,
        func: Callable[..., T],
        *args: Any,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> T:
        self._waiting += 1
        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=self.queue_timeout_seconds)
            except asyncio.TimeoutError as exc:
                raise APIError(
                    self.busy_message,
                    status_code=self.busy_status,
                    code=self.busy_code,
                    details={
                        "executor": self.name,
                        "max_concurrency": self.max_concurrency,
                        "in_flight": self._in_flight,
                        "waiting": self._waiting,
                        "queue_timeout_seconds": self.queue_timeout_seconds,
                    },
                ) from exc
        finally:
            self._waiting = max(0, self._waiting - 1)

        self._in_flight += 1
        loop = asyncio.get_running_loop()
        call = partial(func, *args, **kwargs)
        try:
            selected_timeout = self.default_timeout_seconds if timeout_seconds is None else max(0.1, timeout_seconds)
            try:
                return await asyncio.wait_for(loop.run_in_executor(self._executor, call), timeout=selected_timeout)
            except asyncio.TimeoutError as exc:
                raise APIError(
                    self.timeout_message,
                    status_code=self.timeout_status,
                    code=self.timeout_code,
                    details={
                        "executor": self.name,
                        "timeout_seconds": selected_timeout,
                    },
                ) from exc
        finally:
            self._in_flight = max(0, self._in_flight - 1)
            self._semaphore.release()

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "max_concurrency": self.max_concurrency,
            "in_flight": self._in_flight,
            "waiting": self._waiting,
            "available_slots": max(0, self.max_concurrency - self._in_flight),
            "queue_timeout_seconds": self.queue_timeout_seconds,
            "default_timeout_seconds": self.default_timeout_seconds,
        }

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class ServiceRuntime:
    def __init__(self, config: RuntimeConfig | None = None) -> None:
        self.config = config or RuntimeConfig.from_env()
        self.ocr = BoundedExecutor(
            name="ocr",
            max_concurrency=self.config.ocr_max_concurrency,
            queue_timeout_seconds=self.config.ocr_queue_timeout_seconds,
            default_timeout_seconds=self.config.ocr_request_timeout_seconds,
            busy_message="OCR service is busy, retry later",
            busy_code="service_busy",
            busy_status=429,
            timeout_message="OCR processing timed out",
            timeout_code="processing_timeout",
            timeout_status=504,
        )
        self.notify = BoundedExecutor(
            name="notify",
            max_concurrency=self.config.notify_max_concurrency,
            queue_timeout_seconds=self.config.notify_queue_timeout_seconds,
            default_timeout_seconds=self.config.notify_timeout_seconds,
            busy_message="Notification queue is busy, skipping handoff",
            busy_code="notification_busy",
            busy_status=503,
            timeout_message="Notification timed out",
            timeout_code="notification_timeout",
            timeout_status=504,
        )

    async def run_ocr(
        self,
        func: Callable[..., T],
        *args: Any,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> T:
        return await self.ocr.run(func, *args, timeout_seconds=timeout_seconds, **kwargs)

    async def run_notify(
        self,
        func: Callable[..., T],
        *args: Any,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> T:
        return await self.notify.run(func, *args, timeout_seconds=timeout_seconds, **kwargs)

    def snapshot(self) -> dict[str, Any]:
        return {
            "ocr": self.ocr.snapshot(),
            "notify": self.notify.snapshot(),
        }

    def shutdown(self) -> None:
        self.ocr.shutdown()
        self.notify.shutdown()


service_runtime = ServiceRuntime()
