# Watchtower — Architecture (Phase 0)

## 1. Design goals for this phase

- Single-process, self-hosted, SQLite-backed. No external message
  broker, no multi-node coordination. Simplicity now; the schema
  and module boundaries should not have to be blown up later.
- Clear separation between **raw observation** (what a watcher saw)
  and **derived state** (what the detector concluded). This is the
  single most important boundary in the system — Phase 3 and Phase 4
  build entirely on top of Phase 1/2 data without needing to touch
  Phase 1/2 tables.
- SQLite is single-writer-friendly but not concurrency-friendly.
  We accept this constraint explicitly rather than fight it (see
  §4).

## 2. Components

```
                         ┌─────────────┐
                         │  config.yaml │
                         └──────┬──────┘
                                │ loaded at startup
                                ▼
┌───────────┐   schedules   ┌───────────┐
│ Scheduler │──────────────▶│  Watchers │
│ (asyncio) │               │ HTTP / Log│
└─────┬─────┘               └─────┬─────┘
      │                           │ raw observations
      │                           ▼
      │                     ┌───────────┐
      │                     │  Storage  │◀── single aiosqlite
      │                     │ (SQLite)  │    connection, WAL mode,
      │                     └─────┬─────┘    write-queue serialized
      │                           │
      │  periodic pass            │ raw observations
      ▼                           ▼
┌───────────┐   incident    ┌───────────┐
│ Detector  │──────────────▶│  Storage  │ (baselines, incidents,
│ (baseline │   state       │           │  error_signatures)
│  + scoring)│  changes     └─────┬─────┘
└───────────┘                     │
      │ on new/resolved/escalated │
      ▼                           │
┌───────────┐                     │ read-only queries
│ Notifier  │                     ▼
│ (webhook) │               ┌───────────┐        ┌────────────┐
└───────────┘               │  FastAPI  │◀──────▶│React Dash- │
                             │  (REST +  │  HTTP/  │board       │
                             │   SSE)    │  SSE    └────────────┘
                             └───────────┘
```

### Scheduler
- Single asyncio event loop. Reads `config.yaml` at startup, builds
  one `asyncio.Task` per service using `asyncio.sleep`-based
  intervals (not `cron` — intervals are simple, per-service, and
  jitter-friendly, which matters once we have 10+ services hitting
  external URLs at synchronized times).
- The scheduler does not know about HTTP vs. log distinctions — it
  just owns timing and calls `watcher.run_once(service)`.

### Watchers
- `HTTPWatcher`: performs the request, measures latency, records
  status code / body size / error. Stateless per call.
- `LogWatcher`: NOT scheduler-driven in the same way — it's a
  long-running tail task (or a passive HTTP ingestion endpoint for
  push-based logs), started once at boot per configured log source,
  not re-invoked on an interval. This is a deliberate asymmetry:
  HTTP checks are pull/interval-based, log tailing is push/stream-based.
  Phase 0 schema must accommodate both without changes — it does,
  because both simply produce rows in their respective raw tables.

### Storage
- SQLite file, WAL mode (`PRAGMA journal_mode=WAL`) so readers
  (API) don't block on writers (scheduler/watchers/detector).
- All writes go through a single `aiosqlite` connection guarded by
  an `asyncio.Lock`, wrapped in a small `Storage` class. This avoids
  "database is locked" errors that plague naive multi-connection
  SQLite usage, at the cost of write throughput — acceptable for a
  monitoring tool writing tens of rows per second at most.
- Reads (from the API layer) use a separate read-only connection
  pool (SQLite supports concurrent readers fine under WAL).

### Detector
- Runs as its own periodic background task (independent interval
  from any single service's check interval — e.g. every 30s it
  looks at whatever new data has landed).
- Reads raw tables, updates `baselines` and `error_signatures`,
  and writes to `incidents` when it concludes a state change.
- Never touches `health_checks` or `log_events` directly for
  anything but reading — it must not be able to corrupt raw
  observation history. This separation is what makes it safe to
  later rewrite detection logic (Phase 3) without a data migration.

### Notifier
- Subscribes (in-process, simple callback/event) to incident state
  transitions emitted by the Detector. Only fires on transitions
  (`opened`, `resolved`, `escalated`), never on "still ongoing" —
  the de-duplication logic lives in the Detector's incident
  read-modify-write, not in the Notifier itself, because the
  Detector is the only component that actually knows whether
  something changed.

### API / Dashboard
- FastAPI serves REST endpoints reading from the read-only SQLite
  connection, plus an SSE endpoint that streams new rows as they're
  detected via a simple in-process pub/sub (asyncio.Queue) fed by
  Storage on every write. React dashboard is a pure consumer of
  this API — no direct DB access.

## 3. Data flow summary

1. Scheduler fires → Watcher produces one raw observation → Storage
   writes it to `health_checks` or `log_events`.
2. Detector wakes on its own interval → reads recent raw rows for
   each service → updates EMA baseline in `baselines` and/or
   registers new hash in `error_signatures` → if score crosses
   threshold, opens/updates/resolves a row in `incidents`.
3. Incident state transition → Notifier fires webhook.
4. API reads all of the above on demand; SSE pushes deltas.

## 4. SQLite concurrency strategy (explicit, since it'll bite us if unstated)

- WAL mode, `synchronous=NORMAL` (durability tradeoff acceptable for
  monitoring data — a lost write on power-loss is not catastrophic).
- One writer connection for the whole process, serialized via lock.
  This is a deliberate bottleneck that we accept in Phase 0. If it
  ever becomes a real throughput problem (unlikely at this scale),
  the fix is a write-behind queue, not switching databases.
- Multiple reader connections for the API layer are fine under WAL.

## 5. Migration strategy

- A `schema_migrations` table (`version INTEGER PRIMARY KEY,
  applied_at TEXT`) tracks applied migrations.
- Each migration is a numbered `.sql` file (`001_init.sql`,
  `002_...sql`, ...). On startup, Storage checks the max applied
  version and runs any newer files in order, inside a transaction.
- Phase 0 ships `001_init.sql` (the schema below). This is expected
  to be the *last* time the schema is written by hand as one file —
  from here on, changes are additive migrations.

## 6. Config schema (see `config.yaml` for a worked example)

Top-level keys:
- `storage`: path to the SQLite file.
- `services`: list of service definitions. Each has a `type`
  (`http` or `log`), a `name`, type-specific fields, and an optional
  `notify` list of channel names.
- `notifications`: named webhook channel definitions, referenced by
  name from services (many-to-many: a service can notify multiple
  channels, a channel can serve multiple services).
- `detector`: global defaults for baseline window, stddev thresholds,
  cold-start minimum sample count — overridable per service.

## 7. Known open questions deferred to later phases

- Exact EMA smoothing factor / cold-start sample threshold — Phase 3.
- Webhook payload format beyond "generic + Discord-compatible" —
  Phase 4.
- Auth strategy for the dashboard — Phase 8 (explicitly deferred,
  not forgotten).
- Log rotation detection strategy (inode-based vs. size-based) —
  Phase 2, but the schema already has `source_file` on `log_events`
  so a rotation doesn't lose provenance regardless of how it's
  detected.
