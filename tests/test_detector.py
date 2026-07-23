"""
Detector tests -- the pytest equivalent of tests/phase3_detector_test.py.
Each scenario gets its own test function and its own fresh Storage (via
the `storage` fixture in conftest.py), so a failure in one doesn't
obscure the others and pytest can report exactly which scenario broke.

These are deterministic (seeded random data), not mocked -- the Detector,
Storage, and SQLite are all real; only the data is synthetic.
"""

from __future__ import annotations

import random

import pytest

from app.config import DetectorConfig, DetectorOverride, HTTPCheckConfig, LogConfig, ServiceConfig
from app.detector import Detector


def make_http_service(name: str) -> ServiceConfig:
    return ServiceConfig(
        name=name, type="http", enabled=True,
        check=HTTPCheckConfig(url="http://example.invalid/", interval_seconds=10, timeout_seconds=5),
    )


async def insert_normal_checks(storage, service_id, n, mean, stddev, rng):
    for _ in range(n):
        latency = max(1.0, rng.gauss(mean, stddev))
        await storage.insert_health_check(service_id, 200, latency, 512, True, None)


@pytest.fixture
def detector_cfg():
    return DetectorConfig(
        run_interval_seconds=30, ema_alpha=0.2, cold_start_min_samples=20,
        latency_stddev_threshold=3.0, error_rate_stddev_threshold=3.0,
    )


async def test_cold_start_and_injected_latency_anomaly(storage, detector_cfg):
    svc = make_http_service("latency-anomaly-svc")
    detector = Detector(storage, detector_cfg, [svc])
    service_id = await storage.get_or_create_service(svc.name, "http", "{}")
    rng = random.Random(42)

    # Cycle 1: 15 normal points, still cold-start (<20 samples) -> no events.
    await insert_normal_checks(storage, service_id, 15, mean=100, stddev=8, rng=rng)
    events = await detector.run_once()
    assert len(events) == 0, "cold-start should suppress scoring even on fresh data"

    # Cycle 2: +10 normal points, crosses the cold-start boundary mid-batch
    # -> should STILL be quiet, since the data is genuinely normal.
    await insert_normal_checks(storage, service_id, 10, mean=100, stddev=8, rng=rng)
    events = await detector.run_once()
    assert len(events) == 0, "scoring now active but data is normal -> must not false-positive"

    # Cycle 3: a few more normal points, ONE injected 400ms spike, a few more normal.
    await insert_normal_checks(storage, service_id, 5, mean=100, stddev=8, rng=rng)
    await storage.insert_health_check(service_id, 200, 400.0, 512, True, None)
    await insert_normal_checks(storage, service_id, 4, mean=100, stddev=8, rng=rng)
    events = await detector.run_once()

    latency_events = [e for e in events if e.type == "latency_drift"]
    assert len(latency_events) == 1, "exactly the injected spike should be caught, nothing else"
    assert abs(latency_events[0].details["observed_ms"] - 400.0) < 0.5
    assert len(events) == 1, "no other spurious events this cycle"


async def test_quiet_on_noisy_but_normal_data(storage, detector_cfg):
    svc = make_http_service("noisy-normal-svc")
    detector = Detector(storage, detector_cfg, [svc])
    service_id = await storage.get_or_create_service(svc.name, "http", "{}")
    rng = random.Random(123)

    all_events = []
    for _ in range(5):
        await insert_normal_checks(storage, service_id, 10, mean=200, stddev=15, rng=rng)
        all_events.extend(await detector.run_once())

    assert len(all_events) == 0, "50 genuinely normal observations must produce zero anomalies"


async def test_novel_error_detection_and_normalization(storage, detector_cfg):
    svc = make_http_service("novel-error-svc")
    detector = Detector(storage, detector_cfg, [svc])
    service_id = await storage.get_or_create_service(svc.name, "http", "{}")

    await insert_normal_checks(storage, service_id, 5, mean=50, stddev=5, rng=random.Random(7))
    events = await detector.run_once()
    assert len(events) == 0, "no errors yet -> no novel_error events"

    # First-ever "unexpected status 500", TWICE in the same batch -- only
    # the first occurrence should be flagged novel.
    await insert_normal_checks(storage, service_id, 3, mean=50, stddev=5, rng=random.Random(8))
    await storage.insert_health_check(service_id, 500, 60.0, 128, False, "unexpected status 500")
    await storage.insert_health_check(service_id, 500, 62.0, 128, False, "unexpected status 500")
    events = await detector.run_once()
    novel = [e for e in events if e.type == "novel_error"]
    assert len(novel) == 1
    assert novel[0].details["error_message"] == "unexpected status 500"

    # "unexpected status 502" -- normalization collapses digits, so this
    # hashes to the SAME signature as 500. Documented trade-off: this
    # should NOT be flagged novel.
    await insert_normal_checks(storage, service_id, 4, mean=50, stddev=5, rng=random.Random(9))
    await storage.insert_health_check(service_id, 502, 65.0, 128, False, "unexpected status 502")
    events = await detector.run_once()
    novel = [e for e in events if e.type == "novel_error"]
    assert len(novel) == 0, "502 normalizes to the same signature as 500 -- must not re-flag"

    # A genuinely new error type -- THIS is the required case.
    await insert_normal_checks(storage, service_id, 4, mean=50, stddev=5, rng=random.Random(10))
    await storage.insert_health_check(service_id, None, 3000.0, None, False, "connection reset by peer")
    events = await detector.run_once()
    novel = [e for e in events if e.type == "novel_error"]
    assert len(novel) == 1
    assert novel[0].details["error_message"] == "connection reset by peer"

    sig_rows = await storage.read_query(
        "SELECT normalized_message, occurrence_count FROM error_signatures WHERE service_id = ?;",
        (service_id,),
    )
    assert len(sig_rows) == 2, "exactly 2 distinct signatures: the status-code family, and reset-by-peer"
    status_sig = next(r for r in sig_rows if "status" in r["normalized_message"])
    assert status_sig["occurrence_count"] == 3, "500, 500, and 502 all count toward the same signature"


async def test_log_based_error_rate_and_novel_detection(storage, detector_cfg):
    """The log-event detection path (_process_log_service) is separate
    code from the HTTP path exercised above -- worth testing directly."""
    svc = ServiceConfig(
        name="log-based-svc", type="log", enabled=True,
        log=LogConfig(source="push", parser="json_lines"),
        # error_rate is scored per detector CYCLE, not per line -- the
        # default cold_start_min_samples=20 would need 20 real cycles to
        # warm up. Override for a fast, deterministic test.
        detector=DetectorOverride(cold_start_min_samples=4),
    )
    detector = Detector(storage, detector_cfg, [svc])
    service_id = await storage.get_or_create_service(svc.name, "log", "{}")

    async def insert_log(level, message):
        await storage.insert_log_event(service_id, "2026-07-14T00:00:00.000Z", level, message, message, None)

    for _ in range(3):
        for i in range(9):
            await insert_log("INFO", f"routine line {i}")
        await insert_log("ERROR", "database connection pool exhausted")
        await detector.run_once()

    rate_baseline = await storage.read_query(
        "SELECT sample_count, ema_mean FROM baselines WHERE service_id=? AND metric_type='error_rate';",
        (service_id,),
    )
    assert rate_baseline and abs(rate_baseline[0]["ema_mean"] - 0.1) < 0.05

    # Repeat of the known error -- must NOT re-flag as novel.
    for i in range(9):
        await insert_log("INFO", f"cycle4 routine line {i}")
    await insert_log("ERROR", "database connection pool exhausted")
    events = await detector.run_once()
    assert len([e for e in events if e.type == "novel_error"]) == 0

    # A burst of a genuinely new error type -- both novel AND a rate spike.
    for i in range(5):
        await insert_log("INFO", f"cycle5 routine line {i}")
    for _ in range(5):
        await insert_log("ERROR", "out of memory: killed worker process")
    events = await detector.run_once()
    novel = [e for e in events if e.type == "novel_error"]
    spikes = [e for e in events if e.type == "error_rate_spike"]
    assert len(novel) == 1
    assert novel[0].details["error_message"] == "out of memory: killed worker process"
    assert len(spikes) == 1, "5/10 = 50% vs ~10% baseline should register as a spike"
