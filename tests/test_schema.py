"""
Schema integrity tests -- the pytest equivalent of schema/verify_schema.py,
split into individually-reportable test functions. Builds a fresh SQLite
DB straight from 001_init.sql for each test (via the `conn` fixture) and
exercises real constraint enforcement, not just "does the file exist".
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

SCHEMA_PATH = str(Path(__file__).resolve().parent.parent / "schema" / "001_init.sql")

EXPECTED_TABLES = {
    "schema_migrations", "services", "health_checks", "log_events",
    "baselines", "error_signatures", "incidents", "notifications",
}

EXPECTED_INDEXES = {
    "idx_health_checks_service_time", "idx_log_events_service_time",
    "idx_log_events_level", "idx_incidents_service_status",
    "idx_notifications_incident",
}


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "schema_test.db")
    c = sqlite3.connect(db_path)
    c.execute("PRAGMA foreign_keys = ON;")
    with open(SCHEMA_PATH) as f:
        c.executescript(f.read())
    yield c
    c.close()


def test_all_expected_tables_exist(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
    actual = {r[0] for r in rows}
    assert EXPECTED_TABLES.issubset(actual)


def test_all_expected_indexes_exist(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index';").fetchall()
    actual = {r[0] for r in rows}
    assert EXPECTED_INDEXES.issubset(actual)


def test_migration_is_recorded(conn):
    rows = conn.execute("SELECT version FROM schema_migrations;").fetchall()
    assert [r[0] for r in rows] == [1]


def test_foreign_key_violation_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO health_checks (service_id, success) VALUES (9999, 1);")
        conn.commit()


def test_invalid_service_type_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO services (name, type, config_json) VALUES (?, ?, ?);",
            ("bad-type-service", "carrier_pigeon", "{}"),
        )
        conn.commit()


def test_baseline_unique_constraint(conn):
    cur = conn.execute(
        "INSERT INTO services (name, type, config_json) VALUES (?, ?, ?);",
        ("svc", "http", "{}"),
    )
    service_id = cur.lastrowid
    conn.execute(
        "INSERT INTO baselines (service_id, metric_type, ema_mean, ema_variance, sample_count) "
        "VALUES (?, 'latency', 100, 10, 5);",
        (service_id,),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO baselines (service_id, metric_type, ema_mean, ema_variance, sample_count) "
            "VALUES (?, 'latency', 999, 999, 999);",
            (service_id,),
        )
        conn.commit()


def test_full_relationship_chain_reads_back_correctly(conn):
    """service -> health_check -> incident -> notification, joined, proving
    the FK relationships actually thread together end to end."""
    cur = conn.execute(
        "INSERT INTO services (name, type, config_json) VALUES (?, ?, ?);",
        ("public-api", "http", json.dumps({"url": "https://api.example.com/health"})),
    )
    service_id = cur.lastrowid

    conn.execute(
        "INSERT INTO health_checks (service_id, status_code, latency_ms, response_size, success, error_message) "
        "VALUES (?, 200, 142.3, 512, 1, NULL);",
        (service_id,),
    )
    conn.execute(
        "INSERT INTO health_checks (service_id, status_code, latency_ms, response_size, success, error_message) "
        "VALUES (?, NULL, NULL, NULL, 0, 'connection timeout');",
        (service_id,),
    )

    cur = conn.execute(
        "INSERT INTO incidents (service_id, type, status, severity, details_json) "
        "VALUES (?, 'latency_drift', 'open', 'warning', ?);",
        (service_id, json.dumps({"observed_ms": 900})),
    )
    incident_id = cur.lastrowid

    conn.execute(
        "INSERT INTO notifications (incident_id, channel, status, payload_json) "
        "VALUES (?, 'discord_ops', 'sent', ?);",
        (incident_id, json.dumps({"content": "opened"})),
    )
    conn.commit()

    row = conn.execute(
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
    ).fetchone()

    assert row == ("public-api", 2, "latency_drift", "open", "discord_ops", "sent")


def test_cascade_delete_cleans_up_dependents(conn):
    cur = conn.execute(
        "INSERT INTO services (name, type, config_json) VALUES (?, ?, ?);",
        ("cascade-svc", "http", "{}"),
    )
    service_id = cur.lastrowid
    conn.execute(
        "INSERT INTO health_checks (service_id, success) VALUES (?, 1);", (service_id,)
    )
    conn.execute(
        "INSERT INTO incidents (service_id, type, status, severity) "
        "VALUES (?, 'latency_drift', 'open', 'warning');",
        (service_id,),
    )
    conn.commit()

    conn.execute("DELETE FROM services WHERE id = ?;", (service_id,))
    conn.commit()

    remaining_hc = conn.execute(
        "SELECT COUNT(*) FROM health_checks WHERE service_id = ?;", (service_id,)
    ).fetchone()[0]
    remaining_inc = conn.execute(
        "SELECT COUNT(*) FROM incidents WHERE service_id = ?;", (service_id,)
    ).fetchone()[0]
    assert remaining_hc == 0
    assert remaining_inc == 0
