from __future__ import annotations

import json
import logging

import httpx

from app.config import NotificationChannelConfig, ServiceConfig
from app.storage import Storage

logger = logging.getLogger("watchtower.notifier")


class Notifier:
    def __init__(
        self,
        storage: Storage,
        channels: dict[str, NotificationChannelConfig],
        services: dict[str, ServiceConfig],
    ):
        self.storage = storage
        self.channels = channels
        self.services = services

    async def notify(
        self, incident_id: int, service_name: str, event_type: str,
        severity: str, state: str, details: dict,
    ) -> None:
        svc = self.services.get(service_name)
        channel_names = svc.notify if svc else []
        if not channel_names:
            logger.info(
                "incident %s (%s, %s) on %s has no notify channels configured -- skipping",
                incident_id, event_type, state, service_name,
            )
            return

        for name in channel_names:
            channel = self.channels.get(name)
            if channel is None:
                logger.warning(
                    "service %s references unknown notification channel '%s' -- skipping",
                    service_name, name,
                )
                continue
            payload = self._build_payload(channel, service_name, event_type, severity, state, details)
            status = await self._send(channel, payload)
            await self.storage.insert_notification(incident_id, name, status, json.dumps(payload))
            logger.info(
                "notification incident=%s channel=%s state=%s status=%s",
                incident_id, name, state, status,
            )

    def _build_payload(
        self, channel: NotificationChannelConfig, service_name: str, event_type: str,
        severity: str, state: str, details: dict,
    ) -> dict:
        headline = f"[{state.upper()}] {service_name}: {event_type} ({severity})"
        if channel.format == "discord":
            return {
                "content": headline,
                "embeds": [{
                    "title": f"{service_name} — {event_type}",
                    "description": f"State: {state} | Severity: {severity}",
                    "fields": [{"name": k, "value": str(v), "inline": True} for k, v in details.items()],
                }],
            }
        return {
            "text": headline,
            "service": service_name,
            "type": event_type,
            "severity": severity,
            "state": state,
            "details": details,
        }

    async def _send(self, channel: NotificationChannelConfig, payload: dict) -> str:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(channel.url, json=payload)
            if resp.status_code < 400:
                return "sent"
            logger.warning("webhook to %s returned %s: %s", channel.url, resp.status_code, resp.text[:200])
            return "failed"
        except httpx.RequestError as e:
            logger.warning("webhook to %s failed: %s", channel.url, e)
            return "failed"
