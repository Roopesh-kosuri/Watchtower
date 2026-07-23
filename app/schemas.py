from __future__ import annotations

from pydantic import BaseModel


class ServiceSummary(BaseModel):
    id: int
    name: str
    type: str
    enabled: bool
    status: str  # 'healthy' | 'degraded' | 'critical' -- derived from open incidents
    open_incident_count: int
    last_check_ok: bool | None = None
    last_check_at: str | None = None
    last_latency_ms: float | None = None
    last_log_at: str | None = None
    last_log_level: str | None = None


class MetricPoint(BaseModel):
    timestamp: str
    latency_ms: float | None
    success: bool
    status_code: int | None


class BaselineSnapshot(BaseModel):
    metric_type: str
    ema_mean: float | None
    ema_stddev: float | None
    sample_count: int
    updated_at: str | None = None


class IncidentOut(BaseModel):
    id: int
    service_id: int
    service_name: str
    type: str
    status: str
    severity: str
    opened_at: str
    resolved_at: str | None
    details: dict


class LogEventOut(BaseModel):
    id: int
    timestamp: str
    level: str | None
    message: str
    source_file: str | None


class GlobalLogEventOut(BaseModel):
    id: int
    timestamp: str
    level: str | None
    message: str
    source_file: str | None
    service_id: int
    service_name: str
