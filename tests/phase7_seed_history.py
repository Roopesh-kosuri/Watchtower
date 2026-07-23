"""
Phase 7 test data seeding.

Builds a realistic multi-day history for two services:
  - payments-api (http): mostly healthy, with ONE real incident on
    2026-07-14 (a Tuesday) between 14:02 and 14:31 UTC -- a burst of
    request timeouts causing an error_rate_spike.
  - payments-api-logs (log): routine INFO lines throughout, with a
    matching burst of "database connection pool exhausted" ERROR lines
    during that same window -- the actual causal story.

Also seeds a SECOND, unrelated incident on a different day (2026-07-10)
on a different service, so date-range filtering in the UI test is
answering a real question, not just returning "the only thing in the DB".
"""

import asyncio
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app.storage import Storage  # noqa: E402

DB_PATH = os.path.join(PROJECT_ROOT, "tests", "data", "phase7_test.db")
SCHEMA_PATH = os.path.join(PROJECT_ROOT, "schema", "001_init.sql")


async def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    for ext in ("-wal", "-shm"):
        p = DB_PATH + ext
        if os.path.exists(p):
            os.remove(p)

    storage = Storage(DB_PATH, SCHEMA_PATH)
    await storage.connect()

    payments_id = await storage.get_or_create_service("payments-api", "http", "{}")
    payments_logs_id = await storage.get_or_create_service("payments-api-logs", "log", "{}")
    other_id = await storage.get_or_create_service("checkout-api", "http", "{}")

    async def raw_health_check(service_id, ts, status_code, latency_ms, success, error_message):
        async with storage._write_lock:
            await storage._write_conn.execute(
                """INSERT INTO health_checks
                   (service_id, timestamp, status_code, latency_ms, response_size, success, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?);""",
                (service_id, ts, status_code, latency_ms, 256, int(success), error_message),
            )
            await storage._write_conn.commit()

    async def raw_log_event(service_id, ts, level, message):
        async with storage._write_lock:
            await storage._write_conn.execute(
                """INSERT INTO log_events (service_id, timestamp, level, message, raw, source_file)
                   VALUES (?, ?, ?, ?, ?, ?);""",
                (service_id, ts, level, message, message, None),
            )
            await storage._write_conn.commit()

    async def raw_incident(service_id, type_, status, severity, opened_at, resolved_at, details):
        async with storage._write_lock:
            cur = await storage._write_conn.execute(
                """INSERT INTO incidents
                   (service_id, type, status, severity, opened_at, resolved_at, details_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?);""",
                (service_id, type_, status, severity, opened_at, resolved_at, json.dumps(details)),
            )
            await storage._write_conn.commit()
            return cur.lastrowid

    # --- payments-api: healthy background traffic across several days ---
    healthy_days = ["2026-07-09", "2026-07-11", "2026-07-13", "2026-07-15", "2026-07-17", "2026-07-19"]
    for day in healthy_days:
        for hour in (9, 13, 17):
            await raw_health_check(payments_id, f"{day}T{hour:02d}:00:00.000Z", 200, 82.0, True, None)
            await raw_log_event(payments_logs_id, f"{day}T{hour:02d}:00:05.000Z", "INFO", "request processed")

    # --- THE incident: 2026-07-14, 14:02 - 14:31 UTC ---
    for minute in range(2, 31, 3):
        await raw_health_check(
            payments_id, f"2026-07-14T14:{minute:02d}:00.000Z",
            None, 3000.0, False, "request timed out",
        )
        await raw_log_event(
            payments_logs_id, f"2026-07-14T14:{minute:02d}:05.000Z",
            "ERROR", "database connection pool exhausted",
        )
    # a couple of normal checks before/after, to anchor the window
    await raw_health_check(payments_id, "2026-07-14T13:55:00.000Z", 200, 79.0, True, None)
    await raw_health_check(payments_id, "2026-07-14T14:35:00.000Z", 200, 85.0, True, None)
    await raw_log_event(payments_logs_id, "2026-07-14T13:59:00.000Z", "INFO", "request processed")
    await raw_log_event(payments_logs_id, "2026-07-14T14:33:00.000Z", "INFO", "connection pool recovered")

    incident_id = await raw_incident(
        payments_id, "error_rate_spike", "resolved", "critical",
        "2026-07-14T14:05:00.000Z", "2026-07-14T14:31:00.000Z",
        {
            "observed_rate": 1.0, "baseline_mean_rate": 0.01, "baseline_stddev_rate": 0.02,
            "z_score": 49.5, "failures_in_batch": 10, "total_in_batch": 10,
        },
    )

    # --- an UNRELATED incident on a different day/service, so date
    # filtering in the test is doing real work, not trivial ---
    await raw_incident(
        other_id, "novel_error", "resolved", "warning",
        "2026-07-10T09:15:00.000Z", "2026-07-10T09:20:00.000Z",
        {"error_message": "unexpected status 502"},
    )

    await storage.close()
    print(f"Seeded: payments_id={payments_id}, payments_logs_id={payments_logs_id}, "
          f"other_id={other_id}, incident_id={incident_id}")


if __name__ == "__main__":
    asyncio.run(main())
