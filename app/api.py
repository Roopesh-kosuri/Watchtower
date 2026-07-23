from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.event_bus import EventBus
from app.schemas import (
    BaselineSnapshot, GlobalLogEventOut, IncidentOut, LogEventOut, MetricPoint, ServiceSummary,
)
from app.storage import Storage

router = APIRouter(prefix="/api", tags=["dashboard"])


def _validate_iso(value: str | None, field_name: str) -> None:
    if value is None:
        return
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"'{field_name}' is not a valid ISO8601 timestamp: {value!r}")


async def _get_service_or_404(storage: Storage, service_id: int) -> dict:
    rows = await storage.read_query(
        "SELECT id, name, type, enabled FROM services WHERE id = ?;", (service_id,)
    )
    if not rows:
        raise HTTPException(404, f"no service with id {service_id}")
    return rows[0]


async def _summarize_service(storage: Storage, svc: dict) -> ServiceSummary:
    open_incidents = await storage.read_query(
        "SELECT severity FROM incidents WHERE service_id = ? AND status = 'open';", (svc["id"],)
    )
    open_count = len(open_incidents)
    if open_count == 0:
        status = "healthy"
    elif any(i["severity"] == "critical" for i in open_incidents):
        status = "critical"
    else:
        status = "degraded"

    summary = ServiceSummary(
        id=svc["id"], name=svc["name"], type=svc["type"], enabled=bool(svc["enabled"]),
        status=status, open_incident_count=open_count,
    )
    if svc["type"] == "http":
        latest = await storage.read_query(
            "SELECT timestamp, success, latency_ms FROM health_checks "
            "WHERE service_id = ? ORDER BY id DESC LIMIT 1;",
            (svc["id"],),
        )
        if latest:
            summary.last_check_ok = bool(latest[0]["success"])
            summary.last_check_at = latest[0]["timestamp"]
            summary.last_latency_ms = latest[0]["latency_ms"]
    elif svc["type"] == "log":
        latest = await storage.read_query(
            "SELECT timestamp, level FROM log_events WHERE service_id = ? ORDER BY id DESC LIMIT 1;",
            (svc["id"],),
        )
        if latest:
            summary.last_log_at = latest[0]["timestamp"]
            summary.last_log_level = latest[0]["level"]
    return summary


def _row_to_incident(r: dict) -> IncidentOut:
    return IncidentOut(
        id=r["id"], service_id=r["service_id"], service_name=r["service_name"], type=r["type"],
        status=r["status"], severity=r["severity"], opened_at=r["opened_at"], resolved_at=r["resolved_at"],
        details=json.loads(r["details_json"]) if r["details_json"] else {},
    )


@router.get("/services", response_model=list[ServiceSummary])
async def list_services(request: Request):
    """All monitored services with a computed health status derived from
    open incidents, plus the most recent raw observation for context."""
    storage: Storage = request.app.state.storage
    services = await storage.read_query("SELECT id, name, type, enabled FROM services ORDER BY name;")
    return [await _summarize_service(storage, svc) for svc in services]


@router.get("/services/{service_id}", response_model=ServiceSummary)
async def get_service(service_id: int, request: Request):
    storage: Storage = request.app.state.storage
    svc = await _get_service_or_404(storage, service_id)
    return await _summarize_service(storage, svc)


@router.get("/services/{service_id}/metrics", response_model=list[MetricPoint])
async def get_metrics(
    service_id: int,
    request: Request,
    since: str | None = Query(None, description="ISO8601 lower bound (inclusive)"),
    until: str | None = Query(None, description="ISO8601 upper bound (inclusive)"),
    limit: int = Query(500, ge=1, le=5000),
):
    """Raw per-check latency/success points for an http service.

    Note: there is deliberately no server-side 'error_rate' history mode
    here -- the detector only persists a rolling EMA of error rate, not a
    time series of it. Returning raw success/failure per point lets the
    dashboard compute whatever rolling error-rate view it wants client-side,
    rather than this endpoint fabricating a history that isn't actually
    stored anywhere.
    """
    storage: Storage = request.app.state.storage
    svc = await _get_service_or_404(storage, service_id)
    if svc["type"] != "http":
        raise HTTPException(400, f"service '{svc['name']}' is type={svc['type']}, not http -- no latency metrics")
    _validate_iso(since, "since")
    _validate_iso(until, "until")

    sql = "SELECT timestamp, latency_ms, success, status_code FROM health_checks WHERE service_id = ?"
    params: list = [service_id]
    if since:
        sql += " AND timestamp >= ?"
        params.append(since)
    if until:
        sql += " AND timestamp <= ?"
        params.append(until)
    sql += " ORDER BY timestamp ASC LIMIT ?"
    params.append(limit)

    rows = await storage.read_query(sql, tuple(params))
    return [
        MetricPoint(timestamp=r["timestamp"], latency_ms=r["latency_ms"],
                    success=bool(r["success"]), status_code=r["status_code"])
        for r in rows
    ]


@router.get("/services/{service_id}/baseline", response_model=list[BaselineSnapshot])
async def get_baseline(service_id: int, request: Request):
    """The detector's current learned-normal snapshot for this service."""
    storage: Storage = request.app.state.storage
    await _get_service_or_404(storage, service_id)
    rows = await storage.read_query(
        "SELECT metric_type, ema_mean, ema_variance, sample_count, updated_at "
        "FROM baselines WHERE service_id = ?;",
        (service_id,),
    )
    return [
        BaselineSnapshot(
            metric_type=r["metric_type"], ema_mean=r["ema_mean"],
            ema_stddev=(r["ema_variance"] ** 0.5) if r["ema_variance"] is not None else None,
            sample_count=r["sample_count"], updated_at=r["updated_at"],
        )
        for r in rows
    ]


@router.get("/incidents", response_model=list[IncidentOut])
async def list_incidents(
    request: Request,
    status: str | None = Query(None, description="'open' or 'resolved'"),
    service_id: int | None = Query(None),
    since: str | None = Query(None, description="ISO8601 lower bound on opened_at (inclusive)"),
    until: str | None = Query(None, description="ISO8601 upper bound on opened_at (inclusive)"),
    limit: int = Query(100, ge=1, le=1000),
):
    storage: Storage = request.app.state.storage
    if status is not None and status not in ("open", "resolved"):
        raise HTTPException(400, f"'status' must be 'open' or 'resolved', got {status!r}")
    _validate_iso(since, "since")
    _validate_iso(until, "until")

    sql = "SELECT i.*, s.name AS service_name FROM incidents i JOIN services s ON s.id = i.service_id WHERE 1=1"
    params: list = []
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    if service_id is not None:
        sql += " AND i.service_id = ?"
        params.append(service_id)
    if since:
        sql += " AND i.opened_at >= ?"
        params.append(since)
    if until:
        sql += " AND i.opened_at <= ?"
        params.append(until)
    sql += " ORDER BY i.opened_at DESC LIMIT ?"
    params.append(limit)

    rows = await storage.read_query(sql, tuple(params))
    return [_row_to_incident(r) for r in rows]


@router.get("/services/{service_id}/incidents", response_model=list[IncidentOut])
async def list_service_incidents(
    service_id: int,
    request: Request,
    status: str | None = Query(None, description="'open' or 'resolved'"),
    since: str | None = Query(None, description="ISO8601 lower bound on opened_at (inclusive)"),
    until: str | None = Query(None, description="ISO8601 upper bound on opened_at (inclusive)"),
    limit: int = Query(100, ge=1, le=1000),
):
    storage: Storage = request.app.state.storage
    await _get_service_or_404(storage, service_id)
    if status is not None and status not in ("open", "resolved"):
        raise HTTPException(400, f"'status' must be 'open' or 'resolved', got {status!r}")
    _validate_iso(since, "since")
    _validate_iso(until, "until")

    sql = ("SELECT i.*, s.name AS service_name FROM incidents i JOIN services s ON s.id = i.service_id "
           "WHERE i.service_id = ?")
    params: list = [service_id]
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    if since:
        sql += " AND i.opened_at >= ?"
        params.append(since)
    if until:
        sql += " AND i.opened_at <= ?"
        params.append(until)
    sql += " ORDER BY i.opened_at DESC LIMIT ?"
    params.append(limit)

    rows = await storage.read_query(sql, tuple(params))
    return [_row_to_incident(r) for r in rows]


@router.get("/services/{service_id}/logs", response_model=list[LogEventOut])
async def get_logs(
    service_id: int,
    request: Request,
    level: str | None = Query(None, description="exact level match, case-insensitive"),
    q: str | None = Query(None, description="substring search in message"),
    since: str | None = Query(None, description="ISO8601 lower bound (inclusive)"),
    until: str | None = Query(None, description="ISO8601 upper bound (inclusive)"),
    limit: int = Query(200, ge=1, le=2000),
):
    storage: Storage = request.app.state.storage
    svc = await _get_service_or_404(storage, service_id)
    if svc["type"] != "log":
        raise HTTPException(400, f"service '{svc['name']}' is type={svc['type']}, not log -- no log events")
    _validate_iso(since, "since")
    _validate_iso(until, "until")

    sql = "SELECT id, timestamp, level, message, source_file FROM log_events WHERE service_id = ?"
    params: list = [service_id]
    if level:
        sql += " AND LOWER(level) = LOWER(?)"
        params.append(level)
    if q:
        sql += " AND message LIKE ?"
        params.append(f"%{q}%")
    if since:
        sql += " AND timestamp >= ?"
        params.append(since)
    if until:
        sql += " AND timestamp <= ?"
        params.append(until)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = await storage.read_query(sql, tuple(params))
    return [LogEventOut(**r) for r in rows]


@router.get("/logs", response_model=list[GlobalLogEventOut])
async def search_logs(
    request: Request,
    service_id: int | None = Query(None, description="restrict to one service, or omit to search all"),
    level: str | None = Query(None, description="exact level match, case-insensitive"),
    q: str | None = Query(None, description="substring search in message"),
    since: str | None = Query(None, description="ISO8601 lower bound (inclusive)"),
    until: str | None = Query(None, description="ISO8601 upper bound (inclusive)"),
    limit: int = Query(200, ge=1, le=2000),
):
    """Cross-service log search, for the log explorer. The per-service
    /services/{id}/logs endpoint from Phase 5 is still there for
    service-scoped use; this one exists because a real explorer needs to
    search across services, not just within one at a time."""
    storage: Storage = request.app.state.storage
    if service_id is not None:
        svc = await _get_service_or_404(storage, service_id)
        if svc["type"] != "log":
            raise HTTPException(400, f"service '{svc['name']}' is type={svc['type']}, not log -- no log events")
    _validate_iso(since, "since")
    _validate_iso(until, "until")

    sql = (
        "SELECT le.id, le.timestamp, le.level, le.message, le.source_file, "
        "le.service_id, s.name AS service_name "
        "FROM log_events le JOIN services s ON s.id = le.service_id WHERE 1=1"
    )
    params: list = []
    if service_id is not None:
        sql += " AND le.service_id = ?"
        params.append(service_id)
    if level:
        sql += " AND LOWER(le.level) = LOWER(?)"
        params.append(level)
    if q:
        sql += " AND le.message LIKE ?"
        params.append(f"%{q}%")
    if since:
        sql += " AND le.timestamp >= ?"
        params.append(since)
    if until:
        sql += " AND le.timestamp <= ?"
        params.append(until)
    sql += " ORDER BY le.id DESC LIMIT ?"
    params.append(limit)

    rows = await storage.read_query(sql, tuple(params))
    return [GlobalLogEventOut(**r) for r in rows]


@router.get("/events")
async def stream_events(request: Request):
    """Server-Sent Events stream of new health checks, log events, and
    incident state transitions, for live dashboard updates without polling."""
    bus: EventBus = request.app.state.event_bus
    queue = bus.subscribe()

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"event: {event['event']}\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
