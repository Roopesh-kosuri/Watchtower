from __future__ import annotations

import asyncio
import os

import aiosqlite

from app.event_bus import EventBus


class Storage:
    """
    Single write connection (WAL mode, synchronous=NORMAL), serialized via
    an asyncio.Lock, per the Phase 0 architecture doc. Reads use their own
    short-lived connections, which is safe under WAL.
    """

    def __init__(self, db_path: str, schema_path: str, event_bus: EventBus | None = None):
        self.db_path = db_path
        self.schema_path = schema_path
        self.event_bus = event_bus
        self._write_conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._write_conn = await aiosqlite.connect(self.db_path)
        await self._write_conn.execute("PRAGMA journal_mode=WAL;")
        await self._write_conn.execute("PRAGMA synchronous=NORMAL;")
        await self._write_conn.execute("PRAGMA foreign_keys=ON;")
        await self._run_migrations()

    async def _run_migrations(self) -> None:
        cur = await self._write_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations';"
        )
        table_exists = await cur.fetchone()

        needs_init = True
        if table_exists:
            cur = await self._write_conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations;"
            )
            row = await cur.fetchone()
            needs_init = row[0] == 0

        if needs_init:
            with open(self.schema_path) as f:
                await self._write_conn.executescript(f.read())
            await self._write_conn.commit()

    async def close(self) -> None:
        if self._write_conn:
            await self._write_conn.close()

    def _publish(self, event: dict) -> None:
        if self.event_bus:
            self.event_bus.publish(event)

    async def get_or_create_service(self, name: str, type_: str, config_json: str) -> int:
        async with self._write_lock:
            cur = await self._write_conn.execute(
                "SELECT id FROM services WHERE name = ?;", (name,)
            )
            row = await cur.fetchone()
            if row:
                return row[0]
            cur = await self._write_conn.execute(
                "INSERT INTO services (name, type, config_json) VALUES (?, ?, ?);",
                (name, type_, config_json),
            )
            await self._write_conn.commit()
            return cur.lastrowid

    async def insert_health_check(
        self,
        service_id: int,
        status_code: int | None,
        latency_ms: float | None,
        response_size: int | None,
        success: bool,
        error_message: str | None,
    ) -> None:
        async with self._write_lock:
            await self._write_conn.execute(
                """INSERT INTO health_checks
                   (service_id, status_code, latency_ms, response_size, success, error_message)
                   VALUES (?, ?, ?, ?, ?, ?);""",
                (service_id, status_code, latency_ms, response_size, int(success), error_message),
            )
            await self._write_conn.commit()
        self._publish({
            "event": "health_check", "service_id": service_id, "success": bool(success),
            "latency_ms": latency_ms, "status_code": status_code,
        })

    async def insert_log_event(
        self,
        service_id: int,
        timestamp: str,
        level: str | None,
        message: str,
        raw: str,
        source_file: str | None,
    ) -> None:
        async with self._write_lock:
            await self._write_conn.execute(
                """INSERT INTO log_events
                   (service_id, timestamp, level, message, raw, source_file)
                   VALUES (?, ?, ?, ?, ?, ?);""",
                (service_id, timestamp, level, message, raw, source_file),
            )
            await self._write_conn.commit()
        self._publish({
            "event": "log_event", "service_id": service_id, "level": level,
            "message": message[:200],
        })

    async def upsert_baseline(
        self,
        service_id: int,
        metric_type: str,
        ema_mean: float | None,
        ema_variance: float | None,
        sample_count: int,
    ) -> None:
        async with self._write_lock:
            await self._write_conn.execute(
                """INSERT INTO baselines
                   (service_id, metric_type, ema_mean, ema_variance, sample_count, updated_at)
                   VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                   ON CONFLICT(service_id, metric_type) DO UPDATE SET
                     ema_mean=excluded.ema_mean,
                     ema_variance=excluded.ema_variance,
                     sample_count=excluded.sample_count,
                     updated_at=excluded.updated_at;""",
                (service_id, metric_type, ema_mean, ema_variance, sample_count),
            )
            await self._write_conn.commit()

    async def insert_error_signature(
        self, service_id: int, sig_hash: str, normalized_message: str
    ) -> None:
        async with self._write_lock:
            await self._write_conn.execute(
                """INSERT INTO error_signatures (service_id, signature_hash, normalized_message)
                   VALUES (?, ?, ?);""",
                (service_id, sig_hash, normalized_message),
            )
            await self._write_conn.commit()

    async def touch_error_signature(self, service_id: int, sig_hash: str) -> None:
        async with self._write_lock:
            await self._write_conn.execute(
                """UPDATE error_signatures
                   SET last_seen = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                       occurrence_count = occurrence_count + 1
                   WHERE service_id = ? AND signature_hash = ?;""",
                (service_id, sig_hash),
            )
            await self._write_conn.commit()

    async def insert_incident(self, service_id: int, type_: str, severity: str, details_json: str) -> int:
        async with self._write_lock:
            cur = await self._write_conn.execute(
                """INSERT INTO incidents (service_id, type, status, severity, details_json)
                   VALUES (?, ?, 'open', ?, ?);""",
                (service_id, type_, severity, details_json),
            )
            await self._write_conn.commit()
            incident_id = cur.lastrowid
        self._publish({
            "event": "incident_opened", "incident_id": incident_id, "service_id": service_id,
            "type": type_, "severity": severity,
        })
        return incident_id

    async def update_incident_details(self, incident_id: int, details_json: str) -> None:
        async with self._write_lock:
            await self._write_conn.execute(
                "UPDATE incidents SET details_json = ? WHERE id = ?;", (details_json, incident_id)
            )
            await self._write_conn.commit()

    async def escalate_incident(self, incident_id: int) -> None:
        async with self._write_lock:
            await self._write_conn.execute(
                "UPDATE incidents SET severity = 'critical' WHERE id = ?;", (incident_id,)
            )
            await self._write_conn.commit()
        self._publish({"event": "incident_escalated", "incident_id": incident_id})

    async def resolve_incident(self, incident_id: int) -> None:
        async with self._write_lock:
            await self._write_conn.execute(
                """UPDATE incidents SET status = 'resolved',
                   resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?;""",
                (incident_id,),
            )
            await self._write_conn.commit()
        self._publish({"event": "incident_resolved", "incident_id": incident_id})

    async def touch_incident_notified(self, incident_id: int) -> None:
        async with self._write_lock:
            await self._write_conn.execute(
                """UPDATE incidents SET last_notified_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   WHERE id = ?;""",
                (incident_id,),
            )
            await self._write_conn.commit()

    async def insert_notification(self, incident_id: int, channel: str, status: str, payload_json: str) -> None:
        async with self._write_lock:
            await self._write_conn.execute(
                """INSERT INTO notifications (incident_id, channel, status, payload_json)
                   VALUES (?, ?, ?, ?);""",
                (incident_id, channel, status, payload_json),
            )
            await self._write_conn.commit()

    async def read_query(self, sql: str, params: tuple = ()) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
