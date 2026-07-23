from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import DetectorConfig, ServiceConfig
from app.storage import Storage

logger = logging.getLogger("watchtower.detector")

_NUMERIC_RE = re.compile(r"\d+")


def normalize_error_message(msg: str) -> str:
    """Collapse incidental numeric variation (ports, retry counts, ids,
    embedded latencies, status codes) so repeated instances of the same KIND
    of error hash to the same signature.

    Stated trade-off: this also collapses e.g. 'unexpected status 500' and
    'unexpected status 502' into one signature. Accepted simplification for
    Phase 3 -- splitting status-code-based errors back into distinct
    signatures is a reasonable later refinement if it turns out to matter.
    """
    return _NUMERIC_RE.sub("#", msg.strip().lower())


def signature_hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class AnomalyEvent:
    service_id: int
    service_name: str
    type: str  # 'latency_drift' | 'error_rate_spike' | 'novel_error'
    severity: str
    details: dict = field(default_factory=dict)
    at: str = field(default_factory=_now_iso)


class Detector:
    """
    See module docstring in the Phase 3 summary for the cold-start and
    false-positive design rationale -- repeated briefly here since this is
    the code that implements it:

    Cold-start: a metric's baseline is updated on every observation from the
    first one seen, but scoring for that metric is suppressed until
    sample_count >= cold_start_min_samples. Novel-error detection is NOT
    cold-start-gated -- a first-ever error type is worth flagging immediately.

    False-positive guards: (1) a minimum-stddev floor under the z-score
    denominator, (2) error-rate scored per-batch rather than per-check,
    (3) a stddev-threshold approach that assumes roughly-Gaussian noise.
    """

    def __init__(self, storage: Storage, detector_cfg: DetectorConfig, services: list[ServiceConfig]):
        self.storage = storage
        self.cfg = detector_cfg
        self.services = {s.name: s for s in services}
        self._last_health_check_id: dict[int, int] = {}
        self._last_log_event_id: dict[int, int] = {}

    def _effective_cfg(self, svc: ServiceConfig) -> DetectorConfig:
        if svc.detector is None:
            return self.cfg
        overrides = svc.detector.model_dump(exclude_none=True)
        return self.cfg.model_copy(update=overrides)

    async def run_once(self) -> list[AnomalyEvent]:
        events: list[AnomalyEvent] = []
        rows = await self.storage.read_query("SELECT id, name, type FROM services WHERE enabled = 1;")
        for row in rows:
            svc_cfg = self.services.get(row["name"])
            if svc_cfg is None:
                continue
            eff_cfg = self._effective_cfg(svc_cfg)
            if row["type"] == "http":
                events.extend(await self._process_http_service(row["id"], row["name"], eff_cfg))
            elif row["type"] == "log":
                events.extend(await self._process_log_service(row["id"], row["name"], eff_cfg))
        return events

    async def _process_http_service(
        self, service_id: int, service_name: str, cfg: DetectorConfig
    ) -> list[AnomalyEvent]:
        events: list[AnomalyEvent] = []
        since_id = self._last_health_check_id.get(service_id, 0)
        rows = await self.storage.read_query(
            "SELECT id, latency_ms, success, error_message FROM health_checks "
            "WHERE service_id = ? AND id > ? ORDER BY id ASC;",
            (service_id, since_id),
        )
        if not rows:
            return events
        self._last_health_check_id[service_id] = rows[-1]["id"]

        # --- latency: score + update per observation, in chronological order ---
        baseline = await self._get_baseline(service_id, "latency")
        for r in rows:
            if r["latency_ms"] is None:
                continue
            result = self._score_and_update(
                baseline, r["latency_ms"], cfg.latency_stddev_threshold, cfg.ema_alpha,
                cfg.cold_start_min_samples,
            )
            if result is not None:
                z, observed, mean, stddev = result
                events.append(AnomalyEvent(
                    service_id=service_id, service_name=service_name, type="latency_drift",
                    severity="critical" if z >= 2 * cfg.latency_stddev_threshold else "warning",
                    details={
                        "observed_ms": round(observed, 2), "baseline_mean_ms": round(mean, 2),
                        "baseline_stddev_ms": round(stddev, 2), "z_score": round(z, 2),
                    },
                ))
        await self._save_baseline(service_id, "latency", baseline)

        # --- error rate: scored once per batch, not per individual check ---
        total = len(rows)
        failures = sum(1 for r in rows if not r["success"])
        rate = failures / total if total else 0.0
        rate_baseline = await self._get_baseline(service_id, "error_rate")
        result = self._score_and_update(
            rate_baseline, rate, cfg.error_rate_stddev_threshold, cfg.ema_alpha,
            cfg.cold_start_min_samples, only_flag_increase=True,
        )
        if result is not None:
            z, observed, mean, stddev = result
            events.append(AnomalyEvent(
                service_id=service_id, service_name=service_name, type="error_rate_spike",
                severity="critical" if z >= 2 * cfg.error_rate_stddev_threshold else "warning",
                details={
                    "observed_rate": round(observed, 3), "baseline_mean_rate": round(mean, 3),
                    "baseline_stddev_rate": round(stddev, 3), "z_score": round(z, 2),
                    "failures_in_batch": failures, "total_in_batch": total,
                },
            ))
        await self._save_baseline(service_id, "error_rate", rate_baseline)

        # --- novel error signatures, from failures in this batch ---
        for r in rows:
            if r["success"] or not r["error_message"]:
                continue
            is_novel = await self._register_error_signature(service_id, r["error_message"])
            if is_novel:
                events.append(AnomalyEvent(
                    service_id=service_id, service_name=service_name, type="novel_error",
                    severity="warning", details={"error_message": r["error_message"]},
                ))
        return events

    async def _process_log_service(
        self, service_id: int, service_name: str, cfg: DetectorConfig
    ) -> list[AnomalyEvent]:
        events: list[AnomalyEvent] = []
        since_id = self._last_log_event_id.get(service_id, 0)
        rows = await self.storage.read_query(
            "SELECT id, level, message FROM log_events WHERE service_id = ? AND id > ? ORDER BY id ASC;",
            (service_id, since_id),
        )
        if not rows:
            return events
        self._last_log_event_id[service_id] = rows[-1]["id"]

        error_levels = {"error", "err", "critical", "fatal"}
        total = len(rows)
        error_rows = [r for r in rows if (r["level"] or "").strip().lower() in error_levels]
        rate = len(error_rows) / total if total else 0.0

        rate_baseline = await self._get_baseline(service_id, "error_rate")
        result = self._score_and_update(
            rate_baseline, rate, cfg.error_rate_stddev_threshold, cfg.ema_alpha,
            cfg.cold_start_min_samples, only_flag_increase=True,
        )
        if result is not None:
            z, observed, mean, stddev = result
            events.append(AnomalyEvent(
                service_id=service_id, service_name=service_name, type="error_rate_spike",
                severity="critical" if z >= 2 * cfg.error_rate_stddev_threshold else "warning",
                details={
                    "observed_rate": round(observed, 3), "baseline_mean_rate": round(mean, 3),
                    "baseline_stddev_rate": round(stddev, 3), "z_score": round(z, 2),
                    "error_lines_in_batch": len(error_rows), "total_lines_in_batch": total,
                },
            ))
        await self._save_baseline(service_id, "error_rate", rate_baseline)

        for r in error_rows:
            is_novel = await self._register_error_signature(service_id, r["message"])
            if is_novel:
                events.append(AnomalyEvent(
                    service_id=service_id, service_name=service_name, type="novel_error",
                    severity="warning", details={"error_message": r["message"]},
                ))
        return events

    # --- shared EMA baseline machinery ---

    async def _get_baseline(self, service_id: int, metric_type: str) -> dict:
        rows = await self.storage.read_query(
            "SELECT ema_mean, ema_variance, sample_count FROM baselines "
            "WHERE service_id = ? AND metric_type = ?;",
            (service_id, metric_type),
        )
        if rows:
            return {"service_id": service_id, "metric_type": metric_type, **rows[0]}
        return {
            "service_id": service_id, "metric_type": metric_type,
            "ema_mean": None, "ema_variance": None, "sample_count": 0,
        }

    async def _save_baseline(self, service_id: int, metric_type: str, baseline: dict) -> None:
        await self.storage.upsert_baseline(
            service_id, metric_type,
            baseline["ema_mean"], baseline["ema_variance"], baseline["sample_count"],
        )

    def _score_and_update(
        self, baseline: dict, observed: float, threshold: float, alpha: float,
        cold_start_min: int, only_flag_increase: bool = False,
    ):
        """Mutates `baseline` in place (EMA update happens unconditionally).
        Returns (z, observed, mean, stddev) if this observation should be
        flagged under the PRE-update baseline, else None.

        The delta used for the VARIANCE update is winsorized (capped at a
        multiple of the current stddev) -- discovered necessary by Phase 4
        testing: without this, a single large anomaly's alpha*delta^2 term
        could inflate the learned variance so much in one step that the
        detector became desensitized to subsequent, similarly-sized real
        anomalies within 2-3 cycles. The MEAN update is NOT capped, so a
        genuine sustained shift is still absorbed over multiple cycles --
        this only tempers the variance side of the update."""
        mean = baseline["ema_mean"]
        variance = baseline["ema_variance"]
        count = baseline["sample_count"]

        stddev = variance ** 0.5 if variance else 0.0
        floor = max(mean * 0.05, 1e-6) if mean else 1e-6
        effective_stddev = max(stddev, floor)

        result = None
        if mean is not None and count >= cold_start_min:
            z = (observed - mean) / effective_stddev
            direction_ok = (observed > mean) if only_flag_increase else True
            if direction_ok and z >= threshold:
                result = (z, observed, mean, stddev)

        if mean is None:
            baseline["ema_mean"] = observed
            baseline["ema_variance"] = 0.0
        else:
            delta = observed - mean
            new_mean = mean + alpha * delta

            cap = max(effective_stddev * 5, floor)
            capped_delta = max(-cap, min(delta, cap))
            new_variance = (
                (1 - alpha) * (variance + alpha * capped_delta * capped_delta)
                if variance is not None else 0.0
            )

            baseline["ema_mean"] = new_mean
            baseline["ema_variance"] = new_variance
        baseline["sample_count"] = count + 1
        return result

    async def _register_error_signature(self, service_id: int, raw_message: str) -> bool:
        """Returns True if this normalized signature has never been seen
        before for this service (i.e. this is a genuinely novel error)."""
        normalized = normalize_error_message(raw_message)
        sig_hash = signature_hash(normalized)
        existing = await self.storage.read_query(
            "SELECT id FROM error_signatures WHERE service_id = ? AND signature_hash = ?;",
            (service_id, sig_hash),
        )
        if existing:
            await self.storage.touch_error_signature(service_id, sig_hash)
            return False
        await self.storage.insert_error_signature(service_id, sig_hash, normalized)
        return True
