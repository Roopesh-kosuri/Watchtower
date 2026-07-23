from __future__ import annotations

import json
import logging

from app.detector import AnomalyEvent, normalize_error_message
from app.notifier import Notifier
from app.storage import Storage

logger = logging.getLogger("watchtower.incident_manager")


class IncidentManager:
    """
    De-duplication is structural, not time-based: a notification fires only
    at one of exactly three state transitions -- opened, resolved, escalated.
    An incident that keeps getting flagged cycle after cycle is matched to
    its already-open row and simply updated (no notification); it only stops
    being 'ongoing' when a cycle produces no matching event for it, at which
    point it resolves (fires once) and any later recurrence opens a genuinely
    NEW incident rather than being silently swallowed forever.
    """

    def __init__(self, storage: Storage, notifier: Notifier):
        self.storage = storage
        self.notifier = notifier

    async def process_cycle(self, events: list[AnomalyEvent]) -> None:
        # If more than one event shares a dedup key within the same cycle
        # (e.g. two latency spikes in one batch), the last one wins as the
        # "current" snapshot -- a design choice, not an accident.
        current: dict[tuple, AnomalyEvent] = {}
        for e in events:
            current[self._key(e)] = e

        open_rows = await self.storage.read_query(
            "SELECT id, service_id, type, severity, details_json FROM incidents WHERE status = 'open';"
        )
        open_by_key: dict[tuple, dict] = {}
        for row in open_rows:
            details = json.loads(row["details_json"]) if row["details_json"] else {}
            open_by_key[self._row_key(row, details)] = row

        matched_open_ids: set[int] = set()

        # 1. Ongoing (and possibly escalating): this cycle's event matches
        #    an already-open incident.
        for key, event in current.items():
            open_row = open_by_key.get(key)
            if open_row is None:
                continue
            matched_open_ids.add(open_row["id"])
            await self.storage.update_incident_details(open_row["id"], json.dumps(event.details))
            if event.severity == "critical" and open_row["severity"] != "critical":
                await self.storage.escalate_incident(open_row["id"])
                await self._notify(open_row["id"], event, "escalated")
            # else: genuinely ongoing, no notification -- this is the de-dup.

        # 2. New: this cycle's event has no matching open incident.
        for key, event in current.items():
            if open_by_key.get(key) is not None:
                continue
            incident_id = await self.storage.insert_incident(
                event.service_id, event.type, event.severity, json.dumps(event.details)
            )
            await self._notify(incident_id, event, "opened")

        # 3. Resolved: an open incident whose key did NOT appear this cycle.
        for key, row in open_by_key.items():
            if row["id"] in matched_open_ids:
                continue
            await self.storage.resolve_incident(row["id"])
            await self._notify_resolved(row)

    def _key(self, event: AnomalyEvent) -> tuple:
        if event.type == "novel_error":
            sig = normalize_error_message(event.details.get("error_message", ""))
            return (event.service_id, event.type, sig)
        return (event.service_id, event.type, None)

    def _row_key(self, row: dict, details: dict) -> tuple:
        if row["type"] == "novel_error":
            sig = normalize_error_message(details.get("error_message", ""))
            return (row["service_id"], row["type"], sig)
        return (row["service_id"], row["type"], None)

    async def _notify(self, incident_id: int, event: AnomalyEvent, state: str) -> None:
        await self.notifier.notify(
            incident_id, event.service_name, event.type, event.severity, state, event.details
        )
        await self.storage.touch_incident_notified(incident_id)

    async def _notify_resolved(self, open_row: dict) -> None:
        service_rows = await self.storage.read_query(
            "SELECT name FROM services WHERE id = ?;", (open_row["service_id"],)
        )
        service_name = service_rows[0]["name"] if service_rows else f"service#{open_row['service_id']}"
        details = json.loads(open_row["details_json"]) if open_row["details_json"] else {}
        await self.notifier.notify(
            open_row["id"], service_name, open_row["type"], open_row["severity"], "resolved", details
        )
        await self.storage.touch_incident_notified(open_row["id"])
