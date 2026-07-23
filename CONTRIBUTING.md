# Contributing to Watchtower

## Getting set up

Follow the [Quickstart](README.md#quickstart) in the README first — if
that doesn't work cleanly, that's a bug worth fixing before anything else.

## Running the test suites

```bash
# Backend
pip install -r requirements-dev.txt
python3 -m pytest tests/ -v
python3 schema/verify_schema.py

# Frontend
cd frontend && npm test
```

CI (`.github/workflows/ci.yml`) runs all of this on push/PR — see the
README's [CI](README.md#ci) section for an honest note on what "runs in
CI" actually means here (the workflow is written and locally simulated,
not yet confirmed against a real Actions run).

## Code layout & extension points

If you want to add a new capability, here's where it plugs in:

- **A new log format**: add a parser function to `app/parsers.py` and
  register it in the `_PARSERS` dict. It must accept `(raw: str,
  log_config: LogConfig) -> dict` and return `{"timestamp", "level",
  "message"}` — never raise on a malformed line; fail safe by storing the
  raw text with `level=None` instead. See the existing `plaintext_regex`
  and `json_lines` parsers for the pattern.
- **A new notification channel type** (beyond `webhook`): extend
  `NotificationChannelConfig` in `app/config.py` with the new `kind`, and
  add the corresponding send logic to `Notifier._send()` in
  `app/notifier.py`.
- **A new anomaly type**: `Detector` in `app/detector.py` currently
  scores `latency`, `error_rate`, and novel-error signatures. A new
  metric needs: a `baselines.metric_type` value (schema allows any string
  that matches the CHECK constraint — you'll need a migration to extend
  it), a scoring path in `Detector`, and a new `incidents.type` enum
  value (same — needs a migration).
- **A new REST endpoint**: add it to `app/api.py`'s router. If it returns
  a new shape, add a response model to `app/schemas.py` rather than
  returning a raw dict — FastAPI's auto-docs are only useful if the
  models are accurate.
- **Frontend**: components live under `frontend/src/components/`. The
  `api.js` module is the only place that should call `fetch()` directly
  — components should import from there, not construct their own request
  paths, so the REST contract stays in one place.

## Design principles this codebase tries to hold to

These came up repeatedly during development and are worth keeping in
mind for new contributions:

1. **A failure in one place shouldn't crash the whole process.** The
   scheduler, log watcher, and detector loop all catch and log exceptions
   per-cycle rather than letting one bad service or one bad line take
   down monitoring for everything else.
2. **Fail loud and early, not silent and late.** Config validation
   collects every problem and reports them together at startup, rather
   than crashing on the first bad field and making a person fix things
   one restart at a time.
3. **State transitions, not polling, drive notifications.** The incident
   lifecycle (`open` → `resolved` → `escalated`) is the mechanism for
   de-duplicating alerts — don't add time-based "don't notify again for
   N minutes" logic on top of it; that's a weaker guarantee than the
   state machine already provides.
4. **Don't invent data the schema doesn't have.** If something isn't
   actually persisted (e.g. there's no historical error-rate time series,
   only a rolling baseline), don't fake an endpoint that pretends
   otherwise — return what's real and let the client compute the rest,
   or say plainly that it isn't available yet.

## A note on the test scripts

`tests/test_*.py` are real pytest tests (see `tests/conftest.py`), but
deliberately not mocked end-to-end: `test_incidents.py` dispatches to a
real embedded HTTP receiver, `test_api.py` runs a real `uvicorn.Server`
in a background thread rather than FastAPI's mocked `TestClient`
transport, because that's what actually caught real bugs during
development (see the git history / phase summaries for two concrete
examples: a variance-inflation bug in the EMA anomaly detector, and a
cross-service log-linkage gap in the incident-to-logs navigation — both
found by testing against something real, not a mock of the thing being
verified). If you add new tests, prefer that same property: a real
SQLite file and a real (if temporary) server process over mocking the
thing you're actually trying to verify.

The remaining `tests/phase*.py` standalone scripts (not matching
`test_*.py`, so pytest ignores them) cover live-integration scenarios —
real scheduler ticks against real external URLs, a deliberately-flaky
local HTTP server — that weren't converted in this pass. See the
README's Testing section for which ones and why.
