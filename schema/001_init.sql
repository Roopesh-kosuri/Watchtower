-- Watchtower schema, migration 001 (Phase 0 baseline)
-- Applied inside a single transaction by Storage on first boot.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------
-- Migration bookkeeping
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------
-- services: one row per monitored thing, whether HTTP check,
-- log source, or (future) both. type-specific config lives in
-- config_json rather than as columns, since the shape differs by
-- type and we don't want a wide sparse table.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS services (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    type         TEXT NOT NULL CHECK (type IN ('http', 'log')),
    config_json  TEXT NOT NULL,          -- raw JSON blob of type-specific config
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------
-- health_checks: raw observations from HTTPWatcher. Append-only,
-- never mutated after insert. This is ground truth for the
-- detector and for historical charts.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS health_checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id      INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    timestamp       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status_code     INTEGER,             -- NULL if request failed before getting a response
    latency_ms      REAL,                -- NULL if request failed before completion
    response_size   INTEGER,
    success         INTEGER NOT NULL CHECK (success IN (0, 1)),
    error_message   TEXT                 -- NULL on success
);

CREATE INDEX IF NOT EXISTS idx_health_checks_service_time
    ON health_checks (service_id, timestamp);

-- ---------------------------------------------------------------
-- log_events: raw structured observations from LogWatcher.
-- Append-only. source_file retained explicitly so log rotation
-- never loses provenance, regardless of how rotation is detected.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS log_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id   INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    timestamp    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    level        TEXT,                   -- e.g. INFO/WARN/ERROR, NULL if unparseable
    message      TEXT NOT NULL,
    raw          TEXT NOT NULL,          -- untouched original line, for audit/debug
    source_file  TEXT                    -- path at time of ingestion; NULL for pushed logs
);

CREATE INDEX IF NOT EXISTS idx_log_events_service_time
    ON log_events (service_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_log_events_level
    ON log_events (service_id, level);

-- ---------------------------------------------------------------
-- baselines: derived state, one row per (service, metric_type).
-- Owned exclusively by the Detector. Mutated in place (upsert) as
-- new data arrives — this is NOT an append-only history table,
-- it's current rolling state. Historical baseline drift, if ever
-- needed, would be a separate table added later without touching
-- this one.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS baselines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id      INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    metric_type     TEXT NOT NULL CHECK (metric_type IN ('latency', 'error_rate')),
    ema_mean        REAL,
    ema_variance    REAL,
    sample_count    INTEGER NOT NULL DEFAULT 0,   -- used for cold-start gating
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (service_id, metric_type)
);

-- ---------------------------------------------------------------
-- error_signatures: distinct normalized error "shapes" seen per
-- service, so the detector can flag genuinely novel error types
-- rather than re-flagging the same recurring error every time.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS error_signatures (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id          INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    signature_hash      TEXT NOT NULL,       -- hash of normalized error message
    normalized_message  TEXT NOT NULL,
    first_seen          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    occurrence_count    INTEGER NOT NULL DEFAULT 1,
    UNIQUE (service_id, signature_hash)
);

-- ---------------------------------------------------------------
-- incidents: the lifecycle object. One row per incident, mutated
-- as it progresses open -> resolved (or escalated in between).
-- This table is what the Notifier watches for state transitions;
-- de-duplication is enforced by updating this row in place rather
-- than inserting a new one per detection cycle.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incidents (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id       INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    type             TEXT NOT NULL CHECK (
                         type IN ('latency_drift', 'error_rate_spike', 'novel_error')
                     ),
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    severity         TEXT NOT NULL DEFAULT 'warning' CHECK (
                         severity IN ('warning', 'critical')
                     ),
    opened_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at      TEXT,
    details_json     TEXT,                -- snapshot of the scoring that triggered/updated it
    last_notified_at TEXT                 -- gates the Notifier's de-dup logic
);

CREATE INDEX IF NOT EXISTS idx_incidents_service_status
    ON incidents (service_id, status);

-- ---------------------------------------------------------------
-- notifications: audit log of what was actually sent. Separate
-- from incidents so an incident can have many notification
-- attempts (e.g. retries) without ambiguity about lifecycle state.
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id   INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    channel       TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
    sent_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    payload_json  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_incident
    ON notifications (incident_id);

INSERT INTO schema_migrations (version) VALUES (1);
