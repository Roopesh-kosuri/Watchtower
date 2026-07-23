from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from app.api import router as api_router
from app.auth import make_auth_dependency
from app.config import ConfigValidationError, load_config
from app.detector import Detector
from app.detector_loop import DetectorLoop
from app.event_bus import EventBus
from app.incident_manager import IncidentManager
from app.log_manager import LogIngestionManager
from app.notifier import Notifier
from app.scheduler import Scheduler
from app.storage import Storage
from app.watchers.http_watcher import HTTPWatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("watchtower.main")

CONFIG_PATH = os.environ.get("WATCHTOWER_CONFIG", "config/config.yaml")
SCHEMA_PATH = os.environ.get("WATCHTOWER_SCHEMA", "schema/001_init.sql")

# Config is loaded and validated at IMPORT time, not inside lifespan. Two
# reasons: (1) a bad config file should fail fast and loud, before the
# process even looks like it's starting up, with ConfigValidationError's
# clear multi-error message -- not a confusing crash on the first request;
# (2) the auth dependency has to be attached to routes when the FastAPI
# app object is CREATED, which happens before lifespan ever runs.
try:
    cfg = load_config(CONFIG_PATH)
except ConfigValidationError as e:
    logger.error(str(e))
    raise

if not cfg.auth.enabled:
    logger.warning(
        "AUTH IS DISABLED. This instance is unauthenticated -- do not expose it "
        "to the public internet like this. Set `auth.enabled: true` in config.yaml "
        "(see README) before deploying anywhere reachable outside your own machine."
    )

auth_dependency = make_auth_dependency(cfg.auth)


@asynccontextmanager
async def lifespan(app: FastAPI):
    event_bus = EventBus()
    storage = Storage(cfg.storage.path, SCHEMA_PATH, event_bus=event_bus)
    await storage.connect()

    watcher = HTTPWatcher()
    scheduler = Scheduler(storage, watcher, cfg.services)
    await scheduler.start()

    log_manager = LogIngestionManager(storage, cfg.services)
    await log_manager.start()

    detector = Detector(storage, cfg.detector, cfg.services)
    notifier = Notifier(storage, cfg.notifications, {s.name: s for s in cfg.services})
    incident_manager = IncidentManager(storage, notifier)
    detector_loop = DetectorLoop(detector, incident_manager, cfg.detector.run_interval_seconds)
    await detector_loop.start()

    app.state.storage = storage
    app.state.config = cfg
    app.state.scheduler = scheduler
    app.state.log_manager = log_manager
    app.state.detector_loop = detector_loop
    app.state.event_bus = event_bus

    yield

    await scheduler.stop()
    await log_manager.stop()
    await detector_loop.stop()
    await storage.close()


app = FastAPI(title="Watchtower", lifespan=lifespan)
app.include_router(api_router, dependencies=[Depends(auth_dependency)])


@app.get("/health")
async def health():
    """Liveness probe for the backend process itself — deliberately left
    unauthenticated (standard practice for load balancer / uptime checks)
    and reveals nothing about monitored services."""
    return {"status": "ok"}


class PushLogsRequest(BaseModel):
    lines: list[str]


@app.post("/ingest/logs/{service_name}", dependencies=[Depends(auth_dependency)])
async def ingest_logs(service_name: str, body: PushLogsRequest, request: Request):
    """Accepts pushed log lines for a `type: log, source: push` service.
    Goes through the exact same LogIngestor.ingest_line() path as the file
    tailer, so parsing behavior can't silently diverge between the two."""
    manager: LogIngestionManager = request.app.state.log_manager
    svc = manager.get_push_service(service_name)
    if svc is None:
        raise HTTPException(
            status_code=404,
            detail=f"no push-type log service named '{service_name}'",
        )
    service_id, log_config = svc
    for line in body.lines:
        await manager.ingestor.ingest_line(service_id, log_config, line, source_file=None)
    return {"ingested": len(body.lines)}
