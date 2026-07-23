import { useEffect, useState } from "react";
import { api } from "../api.js";

const LEVEL_CLASS = {
  error: "log-error", err: "log-error", critical: "log-error", fatal: "log-error",
  warn: "log-warn", warning: "log-warn",
  info: "log-info",
};

function levelClass(level) {
  if (!level) return "log-dim";
  return LEVEL_CLASS[level.toLowerCase()] || "log-dim";
}

export default function LogsView({ services, initialFilter, liveEvent }) {
  const logServices = services.filter((s) => s.type === "log");
  const [filters, setFilters] = useState({
    serviceId: initialFilter?.serviceId || "",
    level: "",
    q: "",
    since: initialFilter?.since || "",
    until: initialFilter?.until || "",
  });
  const [logs, setLogs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // A new nonce means the caller (e.g. an incident's "view logs around
  // this" link) wants us to adopt a fresh filter, even if the values
  // happen to look the same as before.
  useEffect(() => {
    if (!initialFilter) return;
    setFilters((f) => ({
      ...f,
      serviceId: initialFilter.serviceId || "",
      since: initialFilter.since || "",
      until: initialFilter.until || "",
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialFilter?.nonce]);

  useEffect(() => {
    setLoading(true);
    setError(null);
    api
      .searchLogs({
        serviceId: filters.serviceId || undefined,
        level: filters.level || undefined,
        q: filters.q || undefined,
        since: filters.since || undefined,
        until: filters.until || undefined,
        limit: 300,
      })
      .then((data) => {
        setLogs(data);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, [filters]);

  useEffect(() => {
    if (!liveEvent || filters.until) return; // don't disturb a bounded historical search
    const matchesService = !filters.serviceId || String(liveEvent.service_id) === String(filters.serviceId);
    const matchesLevel = !filters.level || (liveEvent.level || "").toLowerCase() === filters.level.toLowerCase();
    const matchesText = !filters.q || (liveEvent.message || "").toLowerCase().includes(filters.q.toLowerCase());
    if (matchesService && matchesLevel && matchesText) {
      const svc = services.find((s) => s.id === liveEvent.service_id);
      setLogs((prev) =>
        [
          {
            id: `live-${Date.now()}-${Math.random()}`,
            timestamp: new Date().toISOString(),
            level: liveEvent.level,
            message: liveEvent.message,
            service_id: liveEvent.service_id,
            service_name: svc?.name || `service#${liveEvent.service_id}`,
          },
          ...prev,
        ].slice(0, 500)
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveEvent]);

  return (
    <div className="view-pane">
      <div className="filter-bar">
        <select
          value={filters.serviceId}
          onChange={(e) => setFilters((f) => ({ ...f, serviceId: e.target.value }))}
          aria-label="Filter by log source"
        >
          <option value="">All log sources</option>
          {logServices.map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
        <input
          type="text"
          placeholder="level (e.g. ERROR)"
          value={filters.level}
          onChange={(e) => setFilters((f) => ({ ...f, level: e.target.value }))}
          aria-label="Filter by level"
        />
        <input
          type="text"
          placeholder="search message…"
          value={filters.q}
          onChange={(e) => setFilters((f) => ({ ...f, q: e.target.value }))}
          aria-label="Search message text"
        />
        <label className="date-field">
          Since
          <input
            type="date"
            value={filters.since ? filters.since.slice(0, 10) : ""}
            onChange={(e) =>
              setFilters((f) => ({ ...f, since: e.target.value ? new Date(e.target.value).toISOString() : "" }))
            }
          />
        </label>
        <label className="date-field">
          Until
          <input
            type="date"
            value={filters.until ? filters.until.slice(0, 10) : ""}
            onChange={(e) =>
              setFilters((f) => ({
                ...f,
                until: e.target.value ? new Date(`${e.target.value}T23:59:59`).toISOString() : "",
              }))
            }
          />
        </label>
      </div>

      {loading ? (
        <div className="placeholder">LOADING…</div>
      ) : error ? (
        <div className="placeholder">ERROR: {error}</div>
      ) : logs.length === 0 ? (
        <div className="placeholder">NO LOG LINES MATCH THESE FILTERS</div>
      ) : (
        <div className="log-list">
          {logs.map((log) => (
            <div key={log.id} className="log-line">
              <span className="log-time">{new Date(log.timestamp).toLocaleString()}</span>
              <span className={`log-level ${levelClass(log.level)}`}>{log.level || "—"}</span>
              <span className="log-service">{log.service_name}</span>
              <span className="log-message">{log.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
