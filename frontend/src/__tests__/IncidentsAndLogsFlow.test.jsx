import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App.jsx";

class FakeEventSource {
  constructor() {
    this.listeners = {};
    this.onopen = null;
    setTimeout(() => this.onopen?.(), 0);
  }
  addEventListener(type, handler) {
    this.listeners[type] = this.listeners[type] || [];
    this.listeners[type].push(handler);
  }
  removeEventListener() {}
  close() {}
}

// This dataset mirrors exactly what the backend contract test verified
// against the real, seeded SQLite database (see tests/phase7_seed_history.py
// and the backend-side assertions run against the live server) -- the
// incident details, failure counts, and causal log messages below are not
// invented for this test, they're the same values the real API returned.

const SERVICES = [
  { id: 1, name: "payments-api", type: "http", enabled: true, status: "healthy",
    open_incident_count: 0, last_check_ok: true, last_check_at: "2026-07-19T17:00:00Z", last_latency_ms: 82 },
  { id: 2, name: "payments-api-logs", type: "log", enabled: true, status: "healthy",
    open_incident_count: 0, last_log_at: "2026-07-19T17:00:05Z", last_log_level: "INFO" },
  { id: 3, name: "checkout-api", type: "http", enabled: true, status: "healthy",
    open_incident_count: 0, last_check_ok: true, last_check_at: "2026-07-19T17:00:00Z", last_latency_ms: 60 },
];

const ALL_INCIDENTS = [
  {
    id: 1, service_id: 1, service_name: "payments-api", type: "error_rate_spike",
    status: "resolved", severity: "critical",
    opened_at: "2026-07-14T14:05:00.000Z", resolved_at: "2026-07-14T14:31:00.000Z",
    details: { observed_rate: 1.0, baseline_mean_rate: 0.01, baseline_stddev_rate: 0.02,
               z_score: 49.5, failures_in_batch: 10, total_in_batch: 10 },
  },
  {
    id: 2, service_id: 3, service_name: "checkout-api", type: "novel_error",
    status: "resolved", severity: "warning",
    opened_at: "2026-07-10T09:15:00.000Z", resolved_at: "2026-07-10T09:20:00.000Z",
    details: { error_message: "unexpected status 502" },
  },
];

const CAUSAL_LOGS = Array.from({ length: 10 }, (_, i) => ({
  id: 100 + i,
  timestamp: `2026-07-14T14:${String(2 + i * 3).padStart(2, "0")}:05.000Z`,
  level: "ERROR",
  message: "database connection pool exhausted",
  service_id: 2,
  service_name: "payments-api-logs",
}));

function withinRange(ts, since, until) {
  if (since && ts < since) return false;
  if (until && ts > until) return false;
  return true;
}

function mockFetch() {
  return vi.fn((url) => {
    const u = new URL(url.toString(), "http://localhost");
    const path = u.pathname;
    const params = u.searchParams;

    if (path === "/api/services") {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(SERVICES) });
    }
    if (path === "/api/incidents") {
      const serviceId = params.get("service_id");
      const since = params.get("since");
      const until = params.get("until");
      const filtered = ALL_INCIDENTS.filter((inc) => {
        if (serviceId && String(inc.service_id) !== serviceId) return false;
        if (!withinRange(inc.opened_at, since, until)) return false;
        return true;
      });
      return Promise.resolve({ ok: true, json: () => Promise.resolve(filtered) });
    }
    if (path === "/api/logs") {
      const serviceId = params.get("service_id");
      const level = params.get("level");
      const since = params.get("since");
      const until = params.get("until");
      const filtered = CAUSAL_LOGS.filter((log) => {
        if (serviceId && String(log.service_id) !== serviceId) return false;
        if (level && log.level.toLowerCase() !== level.toLowerCase()) return false;
        if (!withinRange(log.timestamp, since, until)) return false;
        return true;
      });
      return Promise.resolve({ ok: true, json: () => Promise.resolve(filtered) });
    }
    if (path.match(/\/api\/services\/\d+\/metrics/) || path.match(/\/api\/services\/\d+\/baseline/)) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve([]) });
    }
    return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({ detail: "not found" }) });
  });
}

describe("Realistic scenario: find why payments-api went down last Tuesday", () => {
  beforeEach(() => {
    global.EventSource = FakeEventSource;
    global.fetch = mockFetch();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("answers the question in a few clicks: Incidents tab -> filter -> expand -> view logs -> see the cause", async () => {
    const user = userEvent.setup();
    render(<App />);

    // Click 1: navigate to the Incidents tab.
    await screen.findAllByText("payments-api"); // initial services view loaded
    await user.click(screen.getByRole("button", { name: "Incidents" }));

    // Before filtering, both incidents are visible (proves the filter is
    // actually doing something, not just showing a pre-narrowed list).
    // Scoped to .incident-list specifically -- "checkout-api" also
    // legitimately appears as an <option> in the service filter dropdown,
    // so an unscoped query is genuinely ambiguous, not a bug in the app.
    await waitFor(() => {
      const list = document.querySelector(".incident-list");
      expect(list).not.toBeNull();
      expect(within(list).getByText("checkout-api")).toBeInTheDocument();
    });
    expect(within(document.querySelector(".incident-list")).getAllByText("payments-api").length).toBe(1);

    // Click 2 & 3: filter by service, then by date.
    const serviceSelect = screen.getByLabelText("Filter by service");
    await user.selectOptions(serviceSelect, "1");

    const filterBar = serviceSelect.closest(".filter-bar");
    const [sinceInput, untilInput] = within(filterBar).getAllByDisplayValue("");
    fireEvent.change(sinceInput, { target: { value: "2026-07-14" } });
    fireEvent.change(untilInput, { target: { value: "2026-07-14" } });

    // Now only the relevant incident should be showing -- the unrelated
    // checkout-api one from a different day/service is gone from the LIST
    // (it correctly remains in the dropdown, which always lists all
    // services regardless of the current filter).
    await waitFor(() => {
      const list = document.querySelector(".incident-list");
      expect(within(list).queryByText("checkout-api")).not.toBeInTheDocument();
      expect(within(list).getByText("Error Rate Spike")).toBeInTheDocument();
    });
    expect(within(document.querySelector(".incident-list")).getByText("CRITICAL")).toBeInTheDocument();

    // Click 4: expand the incident to see the anomaly snapshot.
    await user.click(screen.getByText("Error Rate Spike"));
    await screen.findByText("49.5"); // the real z-score from the backend-verified data
    // failures_in_batch and total_in_batch are both "10" here, so check the
    // specific labeled value rather than an ambiguous bare "10" text match.
    expect(screen.getByText("Failures In Batch").nextElementSibling).toHaveTextContent("10");

    // Click 5: jump to the logs around this incident. The incident is on
    // payments-api (an http service) -- "view logs around this" correctly
    // searches ALL log sources in the time window rather than assuming
    // payments-api itself has log_events (it doesn't; payments-api-logs
    // is a separate service in this data model).
    await user.click(screen.getByText(/View logs around this incident/));

    // Now on the Logs tab, pre-filtered by time window -- the actual root
    // cause should be visible without the user typing anything else.
    const causalLines = await screen.findAllByText("database connection pool exhausted");
    expect(causalLines.length).toBe(10); // matches failures_in_batch exactly

    // Confirm we actually landed on the Logs tab (not still on Incidents).
    expect(screen.getByRole("button", { name: "Logs" })).toHaveClass("active");
  });
});
