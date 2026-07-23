from __future__ import annotations

import asyncio
import logging
import os

from app.config import LogConfig
from app.log_ingest import LogIngestor

logger = logging.getLogger("watchtower.log_watcher")


class FileLogWatcher:
    """
    Polls a file for new lines, holding an open file handle across polls so
    that a rename-based rotation (the file at `path` gets swapped out from
    under us) doesn't lose data that was written to the old inode between our
    last read and the rotation -- we keep reading from the old fd until it's
    genuinely drained, THEN switch.

    Handles two rotation styles:
      - rename-based (e.g. logrotate 'create'): a NEW file appears at `path`
        with a different inode. Detected via inode comparison.
      - copytruncate-style: the file at `path` keeps the same inode but its
        size drops below our current read position. Detected via size check.

    A trailing partial line (no terminating newline) present in the buffer
    at the moment a rotation is detected is flushed as a final line rather
    than silently dropped -- the old file is gone once rotated, so this is
    the only chance to not lose it.
    """

    def __init__(
        self,
        ingestor: LogIngestor,
        service_id: int,
        service_name: str,
        log_config: LogConfig,
        poll_interval: float = 0.3,
    ):
        self.ingestor = ingestor
        self.service_id = service_id
        self.service_name = service_name
        self.log_config = log_config
        self.poll_interval = poll_interval
        self._fh = None
        self._inode: int | None = None
        self._buffer: str = ""

    async def run(self, stop_event: asyncio.Event) -> None:
        path = self.log_config.path
        while not stop_event.is_set():
            try:
                if self._fh is None:
                    self._open(path)
                await self._poll_once(path)
            except FileNotFoundError:
                logger.warning(
                    "%s: log file not found at %s, will retry", self.service_name, path
                )
                self._fh = None
                self._inode = None
            except Exception:
                logger.exception("%s: unexpected error in file tailer", self.service_name)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass  # normal case: poll interval elapsed

        if self._fh:
            self._fh.close()

    def _open(self, path: str) -> None:
        self._fh = open(path, "r")
        st = os.fstat(self._fh.fileno())
        self._inode = st.st_ino
        logger.info("%s: opened %s (inode=%s)", self.service_name, path, self._inode)

    async def _poll_once(self, path: str) -> None:
        await self._drain_current_fd(path)

        try:
            path_stat = os.stat(path)
        except FileNotFoundError:
            # Path momentarily missing mid-rotation (renamed away, not yet
            # recreated). Keep the old fd open and try again next poll --
            # this is not an error, it's a normal window during rotation.
            return

        if path_stat.st_ino != self._inode:
            logger.info(
                "%s: rotation detected (inode %s -> %s)",
                self.service_name, self._inode, path_stat.st_ino,
            )
            await self._drain_current_fd(path)  # catch any last-moment writes
            if self._buffer:
                # Old file is gone -- this is the last chance to capture its
                # trailing unterminated line instead of losing it.
                await self.ingestor.ingest_line(
                    self.service_id, self.log_config, self._buffer, path
                )
                self._buffer = ""
            self._fh.close()
            self._open(path)
            return

        current_pos = self._fh.tell()
        if path_stat.st_size < current_pos:
            logger.info(
                "%s: truncation detected (read position %d > file size %d)",
                self.service_name, current_pos, path_stat.st_size,
            )
            self._fh.seek(0)
            self._buffer = ""

    async def _drain_current_fd(self, path: str) -> None:
        if self._fh is None:
            return
        chunk = self._fh.read()
        if not chunk:
            return
        self._buffer += chunk
        parts = self._buffer.split("\n")
        self._buffer = parts.pop()  # last element: partial line (or "" if chunk ended in \n)
        for line in parts:
            if line == "":
                continue
            await self.ingestor.ingest_line(self.service_id, self.log_config, line, path)
