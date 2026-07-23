from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from app.config import HTTPCheckConfig


@dataclass
class HTTPCheckResult:
    status_code: int | None
    latency_ms: float | None
    response_size: int | None
    success: bool
    error_message: str | None


class HTTPWatcher:
    """Stateless: one run_once() call = one observation. No retries here —
    retry policy, if we ever want one, belongs at the Scheduler level so it's
    visible in the raw data (each attempt is its own row), not hidden inside
    the watcher."""

    async def run_once(self, check_config: HTTPCheckConfig) -> HTTPCheckResult:
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=check_config.timeout_seconds, follow_redirects=True
            ) as client:
                resp = await client.request(check_config.method, check_config.url)
            latency_ms = (time.perf_counter() - start) * 1000
            success = resp.status_code in check_config.expected_status
            error_message = None if success else f"unexpected status {resp.status_code}"
            return HTTPCheckResult(
                status_code=resp.status_code,
                latency_ms=latency_ms,
                response_size=len(resp.content),
                success=success,
                error_message=error_message,
            )
        except httpx.TimeoutException:
            latency_ms = (time.perf_counter() - start) * 1000
            return HTTPCheckResult(
                status_code=None,
                latency_ms=latency_ms,
                response_size=None,
                success=False,
                error_message="request timed out",
            )
        except httpx.RequestError as e:
            latency_ms = (time.perf_counter() - start) * 1000
            return HTTPCheckResult(
                status_code=None,
                latency_ms=latency_ms,
                response_size=None,
                success=False,
                error_message=f"request error: {e.__class__.__name__}: {e}",
            )
