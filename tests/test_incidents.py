"""
Incident lifecycle + notification tests -- the pytest equivalent of
tests/phase4_incident_test.py. Webhook dispatch is real HTTP to an
embedded local receiver (see the `webhook_receiver` fixture), not
mocked -- this sandbox's network egress can't reach discord.com, but the
payload shape sent is the real Discord webhook format, so a live URL
swap needs no code change.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from app.config import DetectorConfig, DetectorOverride, HTTPCheckConfig, NotificationChannelConfig, ServiceConfig
from app.detector import Detector
from app.incident_manager import IncidentManager
from app.notifier import Notifier


@pytest.fixture
def webhook_receiver():
    received = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"_raw": body.decode(errors="replace")}
            with lock:
                received.append(payload)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    yield SimpleNamespace(url=f"http://127.0.0.1:{port}/webhook", received=received)

    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def make_service(name, notify, cold_start=10, ema_alpha=0.05):
    return ServiceConfig(
        name=name, type="http", enabled=True,
        check=HTTPCheckConfig(url="http://example.invalid/", interval_seconds=10, timeout_seconds=5),
        notify=notify,
        detector=DetectorOverride(cold_start_min_samples=cold_start, ema_alpha=ema_alpha),
    )


async def feed_normal(storage, service_id, n, mean=100.0, stddev=8.0, seed=None):
    import random
    rng = random.Random(seed)
    for _ in range(n):
        latency = max(1.0, rng.gauss(mean, stddev))
        await storage.insert_health_check(service_id, 200, latency, 512, True, None)


async def feed_spike(storage, service_id, value):
    await storage.insert_health_check(service_id, 200, value, 512, True, None)


async def test_incident_opens_dedupes_resolves_and_recurs(storage, webhook_receiver):
    svc = make_service("flaky-svc", notify=["test_webhook"])
    channels = {"test_webhook": NotificationChannelConfig(kind="webhook", url=webhook_receiver.url, format="discord")}
    detector_cfg = DetectorConfig(latency_stddev_threshold=3.0, error_rate_stddev_threshold=3.0)
    detector = Detector(storage, detector_cfg, [svc])
    notifier = Notifier(storage, channels, {svc.name: svc})
    incident_manager = IncidentManager(storage, notifier)
    service_id = await storage.get_or_create_service(svc.name, "http", "{}")

    async def cycle():
        events = await detector.run_once()
        await incident_manager.process_cycle(events)

    # Cold start.
    await feed_normal(storage, service_id, 10, seed=1)
    await cycle()
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (service_id,))
    assert len(incidents) == 0

    # Anomaly starts -> exactly 1 incident, exactly 1 "opened" notification.
    await feed_spike(storage, service_id, 300.0)
    await cycle()
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (service_id,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ?;",
        (service_id,),
    )
    assert len(incidents) == 1 and incidents[0]["status"] == "open"
    assert len(notifications) == 1
    payload = json.loads(notifications[0]["payload_json"])
    assert "content" in payload and "embeds" in payload, "must be Discord-webhook-shaped"
    assert "[OPENED]" in payload["content"]

    # Ongoing for 3 more cycles -- must stay at exactly 1 incident, 1 notification.
    for i in range(3):
        await feed_spike(storage, service_id, 300.0 + i)
        await cycle()
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (service_id,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ?;",
        (service_id,),
    )
    assert len(incidents) == 1, "ongoing anomaly must not create duplicate incidents"
    assert len(notifications) == 1, "ongoing anomaly must not re-notify every cycle -- this IS the de-dup"
    assert incidents[0]["status"] == "open"

    # Recovery -> resolved, exactly 1 more ("resolved") notification.
    await feed_normal(storage, service_id, 1, mean=100.0, stddev=5.0, seed=99)
    await cycle()
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (service_id,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ? ORDER BY n.id;",
        (service_id,),
    )
    assert incidents[0]["status"] == "resolved"
    assert len(notifications) == 2
    assert "[RESOLVED]" in json.loads(notifications[1]["payload_json"])["content"]

    # Recurrence -> a genuinely NEW incident, not silently suppressed forever.
    await feed_spike(storage, service_id, 310.0)
    await cycle()
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ? ORDER BY id;", (service_id,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ?;",
        (service_id,),
    )
    assert len(incidents) == 2
    assert incidents[1]["status"] == "open" and incidents[0]["status"] == "resolved"
    assert len(notifications) == 3

    # And the webhook receiver actually received real HTTP POSTs matching.
    assert len(webhook_receiver.received) == 3
    assert "[OPENED]" in webhook_receiver.received[0]["content"]
    assert "[RESOLVED]" in webhook_receiver.received[1]["content"]
    assert "[OPENED]" in webhook_receiver.received[2]["content"]


async def test_escalation_fires_exactly_once(storage, webhook_receiver):
    svc = make_service("escalating-svc", notify=["test_webhook"], ema_alpha=0.02)
    channels = {"test_webhook": NotificationChannelConfig(kind="webhook", url=webhook_receiver.url, format="discord")}
    detector_cfg = DetectorConfig(latency_stddev_threshold=3.0, error_rate_stddev_threshold=3.0)
    detector = Detector(storage, detector_cfg, [svc])
    notifier = Notifier(storage, channels, {svc.name: svc})
    incident_manager = IncidentManager(storage, notifier)
    service_id = await storage.get_or_create_service(svc.name, "http", "{}")

    async def cycle():
        events = await detector.run_once()
        await incident_manager.process_cycle(events)

    await feed_normal(storage, service_id, 10, mean=100.0, stddev=8.0, seed=2)
    await cycle()

    baseline_row = await storage.read_query(
        "SELECT ema_mean, ema_variance FROM baselines WHERE service_id = ? AND metric_type = 'latency';",
        (service_id,),
    )
    b_mean = baseline_row[0]["ema_mean"]
    b_stddev = max(baseline_row[0]["ema_variance"] ** 0.5, b_mean * 0.05)
    mild_value = b_mean + 4.5 * b_stddev
    extreme_value = b_mean + 12 * b_stddev

    # Mild spike -> warning-level incident.
    await feed_spike(storage, service_id, mild_value)
    await cycle()
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (service_id,))
    assert len(incidents) == 1 and incidents[0]["severity"] == "warning"

    # Extreme spike while still open -> escalates, does NOT open a 2nd incident.
    await feed_spike(storage, service_id, extreme_value)
    await cycle()
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (service_id,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ? ORDER BY n.id;",
        (service_id,),
    )
    assert len(incidents) == 1
    assert incidents[0]["severity"] == "critical"
    assert len(notifications) == 2, "opened (warning) + escalated -- exactly 2, not 1 and not 3"
    assert "[ESCALATED]" in json.loads(notifications[1]["payload_json"])["content"]


async def test_no_notify_channels_configured_produces_no_notifications(storage, webhook_receiver):
    svc = make_service("silent-svc", notify=[])
    detector_cfg = DetectorConfig(latency_stddev_threshold=3.0, error_rate_stddev_threshold=3.0)
    detector = Detector(storage, detector_cfg, [svc])
    notifier = Notifier(storage, {}, {svc.name: svc})
    incident_manager = IncidentManager(storage, notifier)
    service_id = await storage.get_or_create_service(svc.name, "http", "{}")

    await feed_normal(storage, service_id, 10, mean=100.0, stddev=8.0, seed=3)
    await incident_manager.process_cycle(await detector.run_once())
    await feed_spike(storage, service_id, 300.0)
    await incident_manager.process_cycle(await detector.run_once())

    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (service_id,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ?;",
        (service_id,),
    )
    assert len(incidents) == 1, "incident still recorded even with no notify channels"
    assert len(notifications) == 0, "no channels configured -> zero notification rows, not a crash"


async def test_unknown_notify_channel_does_not_crash(storage, webhook_receiver):
    svc = make_service("misconfigured-svc", notify=["does-not-exist"])
    detector_cfg = DetectorConfig(latency_stddev_threshold=3.0, error_rate_stddev_threshold=3.0)
    detector = Detector(storage, detector_cfg, [svc])
    notifier = Notifier(storage, {}, {svc.name: svc})  # no channels defined at all
    incident_manager = IncidentManager(storage, notifier)
    service_id = await storage.get_or_create_service(svc.name, "http", "{}")

    await feed_normal(storage, service_id, 10, mean=100.0, stddev=8.0, seed=4)
    await incident_manager.process_cycle(await detector.run_once())
    await feed_spike(storage, service_id, 300.0)

    # Must not raise.
    await incident_manager.process_cycle(await detector.run_once())

    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (service_id,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ?;",
        (service_id,),
    )
    assert len(incidents) == 1
    assert len(notifications) == 0
