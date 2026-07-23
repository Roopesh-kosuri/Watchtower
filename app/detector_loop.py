from __future__ import annotations

import asyncio
import logging

from app.detector import Detector
from app.incident_manager import IncidentManager

logger = logging.getLogger("watchtower.detector_loop")


class DetectorLoop:
    def __init__(self, detector: Detector, incident_manager: IncidentManager, interval_seconds: float):
        self.detector = detector
        self.incident_manager = incident_manager
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="detector-loop")
        logger.info("detector loop started (interval=%ss)", self.interval_seconds)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                events = await self.detector.run_once()
                if events:
                    logger.info("detector cycle produced %d anomaly event(s)", len(events))
                await self.incident_manager.process_cycle(events)
            except Exception:
                logger.exception("unhandled error in detector loop -- will retry next cycle")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        logger.info("detector loop stopped")
