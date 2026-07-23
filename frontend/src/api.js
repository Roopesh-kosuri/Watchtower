import { useEffect, useRef, useState } from "react";

async function getJSON(path) {
  const res = await fetch(path);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

export const api = {
  listServices: () => getJSON("/api/services"),
  getService: (id) => getJSON(`/api/services/${id}`),
  getMetrics: (id, { since, until, limit } = {}) => {
    const params = new URLSearchParams();
    if (since) params.set("since", since);
    if (until) params.set("until", until);
    if (limit) params.set("limit", limit);
    const qs = params.toString();
    return getJSON(`/api/services/${id}/metrics${qs ? `?${qs}` : ""}`);
  },
  getBaseline: (id) => getJSON(`/api/services/${id}/baseline`),
  listIncidents: ({ serviceId, status, since, until, limit } = {}) => {
    const params = new URLSearchParams();
    if (serviceId) params.set("service_id", serviceId);
    if (status) params.set("status", status);
    if (since) params.set("since", since);
    if (until) params.set("until", until);
    if (limit) params.set("limit", limit);
    const qs = params.toString();
    return getJSON(`/api/incidents${qs ? `?${qs}` : ""}`);
  },
  searchLogs: ({ serviceId, level, q, since, until, limit } = {}) => {
    const params = new URLSearchParams();
    if (serviceId) params.set("service_id", serviceId);
    if (level) params.set("level", level);
    if (q) params.set("q", q);
    if (since) params.set("since", since);
    if (until) params.set("until", until);
    if (limit) params.set("limit", limit);
    const qs = params.toString();
    return getJSON(`/api/logs${qs ? `?${qs}` : ""}`);
  },
};

/**
 * Subscribes to the backend's SSE stream and keeps a rolling connection
 * status plus the most recent event. Reconnects automatically on drop
 * (EventSource does this natively) -- exposes `connected` so the UI can
 * show a real LIVE/OFFLINE indicator rather than pretending it's always live.
 */
export function useLiveEvents(onEvent) {
  const [connected, setConnected] = useState(false);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    const source = new EventSource("/api/events");

    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);

    const eventTypes = [
      "health_check", "log_event",
      "incident_opened", "incident_resolved", "incident_escalated",
    ];
    const handlers = eventTypes.map((type) => {
      const handler = (e) => {
        try {
          const data = JSON.parse(e.data);
          onEventRef.current?.(data);
        } catch {
          // malformed event payload -- ignore rather than crash the UI
        }
      };
      source.addEventListener(type, handler);
      return [type, handler];
    });

    return () => {
      handlers.forEach(([type, handler]) => source.removeEventListener(type, handler));
      source.close();
    };
  }, []);

  return { connected };
}
