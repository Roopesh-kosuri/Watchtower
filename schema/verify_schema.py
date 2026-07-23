"""
Phase 0 schema verification.

Builds a fresh SQLite DB from 001_init.sql, then:
  1. confirms every expected table + index exists
  2. confirms foreign keys are enforceable (inserts a bad FK, expects failure)
  3. confirms the CHECK constraints reject bad enum values
  4. inserts one realistic row per table (a full "happy path" service ->
     health_check -> baseline -> error_signature -> incident -> notification
     chain) and reads it back, to prove the relationships actually thread
     together the way the architecture doc claims.
"""

import sqlite3
import os
import sys
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "_verify.db")
SCHEMA_PATH = os.path.join(SCRIPT_DIR, "001_init.sql")

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA foreign_keys = ON;")

with open(SCHEMA_PATH) as f:
    conn.executescript(f.read())

failures = []


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures.append(label)


# --- 1. tables exist -------------------------------------------------
expected_tables = {
    "schema_migrations", "services", "health_checks", "log_events",
    "baselines", "error_signatures", "incidents", "notifications",
}
cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
actual_tables = {row[0] for row in cur.fetchall()}
check(f"all expected tables present ({sorted(expected_tables)})",
      expected_tables.issubset(actual_tables))

# --- 2. indexes exist --------------------------------------------------
expected_indexes = {
    "idx_health_checks_service_time", "idx_log_events_service_time",
    "idx_log_events_level", "idx_incidents_service_status",
    "idx_notifications_incident",
}
cur = conn.execute("SELECT name FROM sqlite_master WHERE type='index';")
actual_indexes = {row[0] for row in cur.fetchall()}
check(f"all expected indexes present ({sorted(expected_indexes)})",
      expected_indexes.issubset(actual_indexes))

# --- 3. migration bookkeeping recorded ---------------------------------
cur = conn.execute("SELECT version FROM schema_migrations;")
versions = [row[0] for row in cur.fetchall()]
check("schema_migrations records version 1", versions == [1])

# --- 4. foreign key enforcement -----------------------------------------
try:
    conn.execute(
        "INSERT INTO health_checks (service_id, success) VALUES (9999, 1);"
    )
    conn.commit()
    check("FK violation on health_checks.service_id is rejected", False)
except sqlite3.IntegrityError:
    check("FK violation on health_checks.service_id is rejected", True)

# --- 5. CHECK constraint enforcement ------------------------------------
try:
    conn.execute(
        "INSERT INTO services (name, type, config_json) VALUES (?, ?, ?);",
        ("bad-type-service", "carrier_pigeon", "{}"),
    )
    conn.commit()
    check("CHECK constraint rejects invalid services.type", False)
except sqlite3.IntegrityError:
    conn.rollback()
    check("CHECK constraint rejects invalid services.type", True)

# --- 6. full happy-path chain -------------------------------------------
cur = conn.execute(
    "INSERT INTO services (name, type, config_json) VALUES (?, ?, ?);",
    ("public-api", "http", json.dumps({"url": "https://api.example.com/health"})),
)
service_id = cur.lastrowid

conn.execute(
    """INSERT INTO health_checks
       (service_id, status_code, latency_ms, response_size, success, error_message)
       VALUES (?, ?, ?, ?, ?, ?);""",
    (service_id, 200, 142.3, 512, 1, None),
)
conn.execute(
    """INSERT INTO health_checks
       (service_id, status_code, latency_ms, response_size, success, error_message)
       VALUES (?, ?, ?, ?, ?, ?);""",
    (service_id, None, None, None, 0, "connection timeout"),
)

conn.execute(
    """INSERT INTO baselines (service_id, metric_type, ema_mean, ema_variance, sample_count)
       VALUES (?, 'latency', ?, ?, ?);""",
    (service_id, 150.0, 20.5, 2),
)

conn.execute(
    """INSERT INTO error_signatures
       (service_id, signature_hash, normalized_message, occurrence_count)
       VALUES (?, ?, ?, ?);""",
    (service_id, "a1b2c3", "connection timeout", 1),
)

cur = conn.execute(
    """INSERT INTO incidents (service_id, type, status, severity, details_json)
       VALUES (?, 'latency_drift', 'open', 'warning', ?);""",
    (service_id, json.dumps({"observed_ms": 900, "baseline_mean": 150.0, "stddev_over": 3.4})),
)
incident_id = cur.lastrowid

conn.execute(
    """INSERT INTO notifications (incident_id, channel, status, payload_json)
       VALUES (?, 'discord_ops', 'sent', ?);""",
    (incident_id, json.dumps({"content": "Incident opened for public-api"})),
)
conn.commit()

# read the chain back via a join, prove it threads together
cur = conn.execute(
    """
    SELECT s.name, hc_count.n, i.type, i.status, n.channel, n.status
    FROM services s
    JOIN (SELECT service_id, COUNT(*) n FROM health_checks GROUP BY service_id) hc_count
      ON hc_count.service_id = s.id
    JOIN incidents i ON i.service_id = s.id
    JOIN notifications n ON n.incident_id = i.id
    WHERE s.id = ?;
    """,
    (service_id,),
)
row = cur.fetchone()
check(
    "full chain (service -> health_checks -> incident -> notification) reads back correctly",
    row == ("public-api", 2, "latency_drift", "open", "discord_ops", "sent"),
)
if row is not None:
    print(f"       -> row: {row}")

# --- 7. UNIQUE constraint on baselines (service_id, metric_type) --------
try:
    conn.execute(
        """INSERT INTO baselines (service_id, metric_type, ema_mean, ema_variance, sample_count)
           VALUES (?, 'latency', 999, 999, 999);""",
        (service_id,),
    )
    conn.commit()
    check("UNIQUE(service_id, metric_type) on baselines is enforced", False)
except sqlite3.IntegrityError:
    conn.rollback()
    check("UNIQUE(service_id, metric_type) on baselines is enforced", True)

# --- 8. cascade delete ----------------------------------------------------
conn.execute("DELETE FROM services WHERE id = ?;", (service_id,))
conn.commit()
cur = conn.execute("SELECT COUNT(*) FROM health_checks WHERE service_id = ?;", (service_id,))
remaining_hc = cur.fetchone()[0]
cur = conn.execute("SELECT COUNT(*) FROM incidents WHERE service_id = ?;", (service_id,))
remaining_inc = cur.fetchone()[0]
check("ON DELETE CASCADE cleans up health_checks and incidents when service deleted",
      remaining_hc == 0 and remaining_inc == 0)

conn.close()

print()
if failures:
    print(f"RESULT: {len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
else:
    print("RESULT: all schema verification checks PASSED")
    sys.exit(0)
