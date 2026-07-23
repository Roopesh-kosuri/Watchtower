import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App.jsx";

let lastEventSourceInstance = null;

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = {};
    this.onopen = null;
    this.onerror = null;
    lastEventSourceInstance = this;
    setTimeout(() => this.onopen?.(), 0);
  }
  addEventListener(type, handler) {
    this.listeners[type] = this.listeners[type] || [];
    this.listeners[type].push(handler);
  }
  removeEventListener(type, handler) {
    this.listeners[type] = (this.listeners[type] || []).filter((h) => h !== handler);
  }
  close() {}
  emit(type, dataObj) {
    (this.listeners[type] || []).forEach((h) => h({ data: JSON.stringify(dataObj) }));
  }
}

const SERVICES_INITIAL = [
  {
    id: 1, name: "healthy-svc", type: "http", enabled: true, status: "healthy",
    open_incident_count: 0, last_check_ok: true, last_check_at: "2026-01-01T00:00:00Z",
    last_latency_ms: 80,
  },
  {
    id: 2, name: "app-logs", type: "log", enabled: true, status: "healthy",
    open_incident_count: 0, last_log_at: "2026-01-01T00:00:00Z", last_log_level: "INFO",
  },
];

// Returned on the SECOND call to /api/services -- simulates a real
// incident having opened server-side between the initial load and a
// later SSE-triggered refetch.
const SERVICES_AFTER_INCIDENT = [
  { ...SERVICES_INITIAL[0], status: "critical", open_incident_count: 1 },
  SERVICES_INITIAL[1],
];

const METRICS_SVC_1 = [
  { timestamp: "2026-01-01T00:00:00Z", latency_ms: 80, success: true, status_code: 200 },
  { timestamp: "2026-01-01T00:00:05Z", latency_ms: 82, success: true, status_code: 200 },
];

const BASELINE_SVC_1 = [
  { metric_type: "latency", ema_mean: 81, ema_stddev: 5, sample_count: 40, updated_at: "2026-01-01T00:00:05Z" },
];

function mockFetch() {
  let servicesCallCount = 0;
  return vi.fn((url) => {
    const path = url.toString();
    if (path === "/api/services") {
      servicesCallCount += 1;
      const body = servicesCallCount === 1 ? SERVICES_INITIAL : SERVICES_AFTER_INCIDENT;
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    }
    if (path.startsWith("/api/services/1/metrics")) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(METRICS_SVC_1) });
    }
    if (path.startsWith("/api/services/1/baseline")) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(BASELINE_SVC_1) });
    }
    return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({ detail: "not found" }) });
  });
}

describe("App live integration (mocked backend, real React tree)", () => {
  beforeEach(() => {
    global.EventSource = FakeEventSource;
    global.fetch = mockFetch();
    lastEventSourceInstance = null;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("loads and displays the real service list from the backend", async () => {
    render(<App />);
    await screen.findAllByText("healthy-svc");
    expect(screen.getByText("app-logs")).toBeInTheDocument();
  });

  it("shows OFFLINE until the SSE connection opens, then flips to LIVE", async () => {
    render(<App />);
    expect(screen.getByText("OFFLINE")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("LIVE")).toBeInTheDocument());
  });

  it("auto-selects the first service and renders its real fetched baseline", async () => {
    render(<App />);
    // Unambiguous: the detail pane's name is an <h1>, unlike the list row.
    await screen.findByRole("heading", { name: "healthy-svc" });
    // baseline mean of 81ms comes from the mocked /api/services/1/baseline
    // response -- if this appears, ServiceDetail genuinely fetched and
    // rendered it, not a hardcoded placeholder.
    await screen.findByText("81ms");
    await screen.findByText("last 2 checks", { exact: false });
  });

  it("a live health_check SSE event appends a new point WITHOUT any manual refresh action", async () => {
    render(<App />);
    await screen.findByText(/last 2 checks/);

    await waitFor(() => expect(lastEventSourceInstance).not.toBeNull());

    // Simulate the backend pushing a brand new real-time observation --
    // nothing in the test calls a refetch or reload function directly.
    act(() => {
      lastEventSourceInstance.emit("health_check", {
        event: "health_check", service_id: 1, success: true, latency_ms: 999, status_code: 200,
      });
    });

    // The chart header interpolates points.length -- if this updates from
    // 2 to 3 purely from the SSE event, the live-update path is real.
    await screen.findByText(/last 3 checks/);
  });

  it("a live incident_opened SSE event updates the service list's status WITHOUT a manual refresh", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "healthy-svc" });
    expect(screen.queryByText("CRIT")).not.toBeInTheDocument();

    await waitFor(() => expect(lastEventSourceInstance).not.toBeNull());

    act(() => {
      lastEventSourceInstance.emit("incident_opened", {
        event: "incident_opened", incident_id: 1, service_id: 1, type: "latency_drift", severity: "critical",
      });
    });

    // The event handler debounces the services refetch by 300ms -- wait
    // past that, then confirm the SECOND (post-incident) server response
    // actually made it into the rendered DOM.
    await waitFor(() => expect(screen.getByText("CRIT")).toBeInTheDocument(), { timeout: 2000 });
  });
});
