"""
Phase 4 incident/notification test harness.

Runs deterministically against a fresh SQLite file, calling Detector.run_once()
and IncidentManager.process_cycle() directly per simulated "cycle" -- no
wall-clock waiting, so results are exact and reproducible.

Webhook dispatch is real HTTP (not mocked): an embedded ThreadingHTTPServer
runs in this same process as a stand-in for Discord's webhook endpoint,
since this sandbox's network egress is allowlisted and can't reach
discord.com directly. The payload shape sent is the real Discord webhook
format (content + embeds) -- swapping in a live URL needs no code change.
"""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app.config import (  # noqa: E402
    DetectorConfig, DetectorOverride, HTTPCheckConfig,
    NotificationChannelConfig, ServiceConfig,
)
from app.detector import Detector  # noqa: E402
from app.incident_manager import IncidentManager  # noqa: E402
from app.notifier import Notifier  # noqa: E402
from app.storage import Storage  # noqa: E402

DB_PATH = os.path.join(PROJECT_ROOT, "tests", "data", "phase4_test.db")
SCHEMA_PATH = os.path.join(PROJECT_ROOT, "schema", "001_init.sql")
WEBHOOK_PORT = 8766

failures: list[str] = []
received_webhooks: list[dict] = []
_lock = threading.Lock()


def check(label: str, condition: bool, extra: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f"  ({extra})" if extra else ""))
    if not condition:
        failures.append(label)


class WebhookReceiver(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"_raw": body.decode(errors="replace")}
        with _lock:
            received_webhooks.append({"path": self.path, "payload": payload})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass


def make_http_service(name: str, notify: list[str], cold_start: int = 10, ema_alpha: float = 0.05) -> ServiceConfig:
    return ServiceConfig(
        name=name, type="http", enabled=True,
        check=HTTPCheckConfig(url="http://example.invalid/", interval_seconds=10, timeout_seconds=5),
        notify=notify,
        detector=DetectorOverride(cold_start_min_samples=cold_start, ema_alpha=ema_alpha),
    )


async def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    for ext in ("-wal", "-shm"):
        p = DB_PATH + ext
        if os.path.exists(p):
            os.remove(p)

    storage = Storage(DB_PATH, SCHEMA_PATH)
    await storage.connect()

    channels = {
        "test_webhook": NotificationChannelConfig(
            kind="webhook", url=f"http://127.0.0.1:{WEBHOOK_PORT}/webhook", format="discord",
        ),
    }

    svc_main = make_http_service("flaky-svc", notify=["test_webhook"])
    svc_escalate = make_http_service("escalating-svc", notify=["test_webhook"], cold_start=10, ema_alpha=0.02)
    svc_silent = make_http_service("silent-svc", notify=[])  # no channels configured
    svc_badchannel = make_http_service("misconfigured-svc", notify=["does-not-exist"])

    services = [svc_main, svc_escalate, svc_silent, svc_badchannel]
    detector_cfg = DetectorConfig(latency_stddev_threshold=3.0, error_rate_stddev_threshold=3.0)
    detector = Detector(storage, detector_cfg, services)
    notifier = Notifier(storage, channels, {s.name: s for s in services})
    incident_manager = IncidentManager(storage, notifier)

    id_main = await storage.get_or_create_service(svc_main.name, "http", "{}")
    id_escalate = await storage.get_or_create_service(svc_escalate.name, "http", "{}")
    id_silent = await storage.get_or_create_service(svc_silent.name, "http", "{}")
    id_badchannel = await storage.get_or_create_service(svc_badchannel.name, "http", "{}")

    async def cycle():
        events = await detector.run_once()
        await incident_manager.process_cycle(events)
        return events

    async def feed_normal(service_id, n, mean=100.0, stddev=8.0, seed=None):
        import random
        rng = random.Random(seed)
        for _ in range(n):
            latency = max(1.0, rng.gauss(mean, stddev))
            await storage.insert_health_check(service_id, 200, latency, 512, True, None)

    async def feed_spike(service_id, value):
        await storage.insert_health_check(service_id, 200, value, 512, True, None)

    # =================================================================
    # SCENARIO: anomaly opens, stays open across several ongoing cycles
    # with NO repeat notification, then resolves with exactly one
    # 'resolved' notification, then recurs as a genuinely new incident.
    # =================================================================
    print("=" * 70)
    print("SCENARIO: open -> ongoing (deduped) -> resolved -> recurs as new")
    print("=" * 70)

    # Cold start.
    await feed_normal(id_main, 10, seed=1)
    await cycle()
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (id_main,))
    check("after cold-start cycle: no incidents yet", len(incidents) == 0)

    # Anomaly starts.
    await feed_spike(id_main, 300.0)
    events = await cycle()
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (id_main,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ?;",
        (id_main,),
    )
    print(f"  cycle [open]: latency_drift events={sum(1 for e in events if e.type=='latency_drift')}, "
          f"incidents={len(incidents)}, notifications={len(notifications)}")
    check("anomaly start: exactly 1 incident created, status=open", len(incidents) == 1 and incidents[0]["status"] == "open")
    check("anomaly start: exactly 1 notification fired (the 'opened' one)", len(notifications) == 1)
    if notifications:
        payload = json.loads(notifications[0]["payload_json"])
        check("notification payload is Discord-shaped (content + embeds)",
              "content" in payload and "embeds" in payload, f"payload keys={list(payload.keys())}")
        check("notification state embedded is 'OPENED'", "[OPENED]" in payload.get("content", ""))

    # Anomaly continues for 3 more cycles -- must NOT create new incidents
    # or fire new notifications.
    for i in range(3):
        await feed_spike(id_main, 300.0 + i)  # slight variation, still clearly anomalous
        events = await cycle()
        z = next((e.details.get("z_score") for e in events if e.type == "latency_drift"), None)
        sev = next((e.severity for e in events if e.type == "latency_drift"), None)
        inc_now = await storage.read_query("SELECT severity FROM incidents WHERE service_id = ?;", (id_main,))
        print(f"    debug ongoing-cycle {i}: event severity={sev} z={z}, "
              f"incident.severity in DB={inc_now[0]['severity'] if inc_now else None}")
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (id_main,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ?;",
        (id_main,),
    )
    print(f"  after 3 more 'ongoing' cycles: incidents={len(incidents)}, notifications={len(notifications)}")
    check("ongoing anomaly: still exactly 1 incident (not 4)", len(incidents) == 1)
    check("ongoing anomaly: still exactly 1 notification (not 4) -- de-dup proven", len(notifications) == 1)
    check("the single incident is still open", incidents[0]["status"] == "open")

    # Recovery: a normal-range observation. Baseline was kept nearly frozen
    # by a small per-service ema_alpha override specifically so recovery
    # cycles land back in "normal" range rather than triggering a NEW
    # anomaly in the opposite direction (a real EMA-baseline tradeoff,
    # noted in the phase summary).
    await feed_normal(id_main, 1, mean=100.0, stddev=5.0, seed=99)
    events = await cycle()
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (id_main,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ? ORDER BY n.id;",
        (id_main,),
    )
    print(f"  cycle [recovery]: latency_drift events={sum(1 for e in events if e.type=='latency_drift')}, "
          f"incidents={len(incidents)} (statuses={[i['status'] for i in incidents]}), "
          f"notifications={len(notifications)}")
    check("recovery: the incident is now resolved", incidents[0]["status"] == "resolved")
    check("recovery: exactly 2 total notifications now (opened + resolved)", len(notifications) == 2)
    if len(notifications) == 2:
        check("notification #2 state is 'RESOLVED'", "[RESOLVED]" in json.loads(notifications[1]["payload_json"])["content"])

    # Recurrence: a fresh spike after resolution should open a BRAND NEW
    # incident, not be silently suppressed forever by the old one.
    await feed_spike(id_main, 310.0)
    await cycle()
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ? ORDER BY id;", (id_main,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ?;",
        (id_main,),
    )
    print(f"  cycle [recurrence]: incidents={len(incidents)} (statuses={[i['status'] for i in incidents]}), "
          f"notifications={len(notifications)}")
    check("recurrence: a SECOND, distinct incident row now exists", len(incidents) == 2)
    check("recurrence: the new incident is open while the old one stays resolved",
          incidents[1]["status"] == "open" and incidents[0]["status"] == "resolved")
    check("recurrence: exactly 3 total notifications now (opened, resolved, opened again)", len(notifications) == 3)

    # =================================================================
    # SCENARIO: escalation -- warning becomes critical while still open.
    # =================================================================
    print()
    print("=" * 70)
    print("SCENARIO: escalation (warning -> critical) fires exactly one 'escalated' notification")
    print("=" * 70)

    await feed_normal(id_escalate, 10, mean=100.0, stddev=8.0, seed=2)
    await cycle()

    baseline_row = await storage.read_query(
        "SELECT ema_mean, ema_variance FROM baselines WHERE service_id = ? AND metric_type = 'latency';",
        (id_escalate,),
    )
    b_mean = baseline_row[0]["ema_mean"]
    b_stddev = max(baseline_row[0]["ema_variance"] ** 0.5, b_mean * 0.05)
    mild_value = b_mean + 4.5 * b_stddev   # targets z in [3, 6) -> 'warning'
    extreme_value = b_mean + 12 * b_stddev  # targets z >= 6 -> 'critical'
    print(f"  learned baseline: mean={b_mean:.1f} stddev={b_stddev:.1f} "
          f"-> mild_value={mild_value:.1f}, extreme_value={extreme_value:.1f}")

    # Mild spike -> warning-level incident.
    await feed_spike(id_escalate, mild_value)
    events = await cycle()
    sev1 = next((e.severity for e in events if e.type == "latency_drift"), None)
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (id_escalate,))
    print(f"  cycle [mild spike]: severity={sev1}, incident status={incidents[0]['severity'] if incidents else None}")
    check("mild spike opens a 'warning' severity incident", len(incidents) == 1 and incidents[0]["severity"] == "warning")

    # Extreme spike while still open -> should escalate to critical, not
    # open a second incident.
    await feed_spike(id_escalate, extreme_value)
    events = await cycle()
    sev2 = next((e.severity for e in events if e.type == "latency_drift"), None)
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (id_escalate,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ? ORDER BY n.id;",
        (id_escalate,),
    )
    print(f"  cycle [extreme spike]: severity={sev2}, incidents={len(incidents)}, "
          f"incident severity now={incidents[0]['severity'] if incidents else None}, "
          f"notifications={[json.loads(n['payload_json'])['content'] for n in notifications]}")
    check("extreme spike does NOT open a second incident", len(incidents) == 1)
    check("the existing incident's severity is now 'critical'", incidents[0]["severity"] == "critical")
    check("exactly 2 notifications total: opened (warning) + escalated", len(notifications) == 2)
    if len(notifications) == 2:
        check("notification #2 state is 'ESCALATED'", "[ESCALATED]" in json.loads(notifications[1]["payload_json"])["content"])

    # =================================================================
    # SCENARIO: misconfiguration edge cases -- must not crash, must not
    # silently pretend to have notified.
    # =================================================================
    print()
    print("=" * 70)
    print("SCENARIO: misconfiguration edge cases")
    print("=" * 70)

    await feed_normal(id_silent, 10, mean=100.0, stddev=8.0, seed=3)
    await cycle()
    await feed_spike(id_silent, 300.0)
    await cycle()
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (id_silent,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ?;",
        (id_silent,),
    )
    print(f"  silent-svc (notify=[]): incidents={len(incidents)}, notifications={len(notifications)}")
    check("service with no notify channels still gets an incident recorded", len(incidents) == 1)
    check("service with no notify channels produces zero notification rows (not a crash, not a fake send)",
          len(notifications) == 0)

    await feed_normal(id_badchannel, 10, mean=100.0, stddev=8.0, seed=4)
    await cycle()
    await feed_spike(id_badchannel, 300.0)
    try:
        await cycle()
        crashed = False
    except Exception as e:
        crashed = True
        print(f"  CRASHED: {e}")
    incidents = await storage.read_query("SELECT * FROM incidents WHERE service_id = ?;", (id_badchannel,))
    notifications = await storage.read_query(
        "SELECT n.* FROM notifications n JOIN incidents i ON i.id = n.incident_id WHERE i.service_id = ?;",
        (id_badchannel,),
    )
    print(f"  misconfigured-svc (unknown channel 'does-not-exist'): crashed={crashed}, "
          f"incidents={len(incidents)}, notifications={len(notifications)}")
    check("unknown notify channel name does not crash the incident manager", not crashed)
    check("incident is still recorded even though its channel is misconfigured", len(incidents) == 1)
    check("no notification row created for the unknown channel", len(notifications) == 0)

    await storage.close()

    print()
    print("=" * 70)
    print(f"Total webhook POSTs actually received by the embedded receiver: {len(received_webhooks)}")
    print("=" * 70)
    for i, w in enumerate(received_webhooks):
        print(f"  [{i}] {w['payload'].get('content', w['payload'])}")

    print()
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("RESULT: all Phase 4 incident/notification checks PASSED")
        sys.exit(0)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", WEBHOOK_PORT), WebhookReceiver)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    asyncio.run(main())
    os._exit(0)
