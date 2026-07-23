from __future__ import annotations

import asyncio
import json
import logging

from app.config import LogConfig, ServiceConfig
from app.log_ingest import LogIngestor
from app.storage import Storage
from app.watchers.log_watcher import FileLogWatcher

logger = logging.getLogger("watchtower.log_manager")


class LogIngestionManager:
    def __init__(self, storage: Storage, services: list[ServiceConfig]):
        self.storage = storage
        self.services = services
        self.ingestor = LogIngestor(storage)
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.push_services: dict[str, tuple[int, LogConfig]] = {}

    async def start(self) -> None:
        for svc in self.services:
            if svc.type != "log" or not svc.enabled:
                continue
            service_id = await self.storage.get_or_create_service(
                svc.name, svc.type, json.dumps(svc.log.model_dump())
            )
            if svc.log.source == "file":
                watcher = FileLogWatcher(self.ingestor, service_id, svc.name, svc.log)
                task = asyncio.create_task(watcher.run(self._stop), name=f"tail:{svc.name}")
                self._tasks.append(task)
                logger.info("started file tailer for %s (%s)", svc.name, svc.log.path)
            elif svc.log.source == "push":
                self.push_services[svc.name] = (service_id, svc.log)
                logger.info("registered push log source for %s", svc.name)
        logger.info("log ingestion manager started with %d file tailer(s)", len(self._tasks))

    def get_push_service(self, name: str) -> tuple[int, LogConfig] | None:
        return self.push_services.get(name)

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("log ingestion manager stopped")
