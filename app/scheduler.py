from __future__ import annotations

import asyncio
import json
import logging

from app.config import ServiceConfig
from app.storage import Storage
from app.watchers.http_watcher import HTTPWatcher

logger = logging.getLogger("watchtower.scheduler")


class Scheduler:
    def __init__(self, storage: Storage, watcher: HTTPWatcher, services: list[ServiceConfig]):
        self.storage = storage
        self.watcher = watcher
        self.services = services
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()

    async def start(self) -> None:
        for svc in self.services:
            if svc.type != "http":
                # Log services are Phase 2's responsibility. Not stubbed here —
                # simply not scheduled, so there's no dead code pretending to work.
                continue
            if not svc.enabled:
                logger.info("skipping disabled service: %s", svc.name)
                continue
            service_id = await self.storage.get_or_create_service(
                svc.name, svc.type, json.dumps(svc.check.model_dump())
            )
            task = asyncio.create_task(self._run_service_loop(svc, service_id), name=f"check:{svc.name}")
            self._tasks.append(task)
        logger.info("scheduler started with %d service task(s)", len(self._tasks))

    async def _run_service_loop(self, svc: ServiceConfig, service_id: int) -> None:
        while not self._stop.is_set():
            try:
                result = await self.watcher.run_once(svc.check)
                await self.storage.insert_health_check(
                    service_id,
                    result.status_code,
                    result.latency_ms,
                    result.response_size,
                    result.success,
                    result.error_message,
                )
                logger.info(
                    "%-20s success=%-5s status=%-4s latency=%6.1fms %s",
                    svc.name,
                    result.success,
                    result.status_code,
                    result.latency_ms if result.latency_ms is not None else -1,
                    ("" if result.success else f"[{result.error_message}]"),
                )
            except Exception:
                # A bug in one service's check must not take down the whole
                # scheduler — log it and keep the loop alive for next cycle.
                logger.exception("unhandled error checking service %s", svc.name)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=svc.check.interval_seconds)
            except asyncio.TimeoutError:
                pass  # normal case: interval elapsed, loop again

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("scheduler stopped")
