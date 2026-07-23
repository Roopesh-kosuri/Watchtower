from __future__ import annotations

from app.config import LogConfig
from app.parsers import parse_line
from app.storage import Storage


class LogIngestor:
    """The single code path both FileLogWatcher and the /ingest/logs API route
    go through, so 'tailed' and 'pushed' logs can never quietly diverge in how
    they're parsed or stored."""

    def __init__(self, storage: Storage):
        self.storage = storage

    async def ingest_line(
        self, service_id: int, log_config: LogConfig, raw: str, source_file: str | None
    ) -> None:
        parsed = parse_line(raw, log_config)
        await self.storage.insert_log_event(
            service_id=service_id,
            timestamp=parsed["timestamp"],
            level=parsed["level"],
            message=parsed["message"],
            raw=raw,
            source_file=source_file,
        )
