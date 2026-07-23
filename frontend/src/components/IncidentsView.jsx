import { useEffect, useState } from "react";
import { api } from "../api.js";
import { detailKeyLabel, formatDuration, incidentTypeLabel } from "../incidentUtils.js";

export default function IncidentsView({ services, refetchTick, onViewLogsAround }) {
  const [filters, setFilters] = useState({ serviceId: "", status: "", since: "", until: "" });
  const [incidents, setIncidents] = useState([]);
  const [expandedId, setExpandedId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    api
      .listIncidents({
        serviceId: filters.serviceId || undefined,
        status: filters.status || undefined,
        since: filters.since ? new Date(filters.since).toISOString() : undefined,
        until: filters.until ? new Date(`${filters.until}T23:59:59`).toISOString() : undefined,
        limit: 200,
      })
      .then((data) => {
        setIncidents(data);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, [filters, refetchTick]);

  const hasActiveFilters = filters.serviceId || filters.status || filters.since || filters.until;

  return (
    <div className="view-pane">
      <div className="filter-bar">
        <select
          value={filters.serviceId}
          onChange={(e) => setFilters((f) => ({ ...f, serviceId: e.target.value }))}
          aria-label="Filter by service"
        >
          <option value="">All services</option>
          {services.map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
        <select
          value={filters.status}
          onChange={(e) => setFilters((f) => ({ ...f, status: e.target.value }))}
          aria-label="Filter by status"
        >
          <option value="">All statuses</option>
          <option value="open">Open</option>
          <option value="resolved">Resolved</option>
        </select>
        <label className="date-field">
          Since
          <input
            type="date"
            value={filters.since}
            onChange={(e) => setFilters((f) => ({ ...f, since: e.target.value }))}
          />
        </label>
        <label className="date-field">
          Until
          <input
            type="date"
            value={filters.until}
            onChange={(e) => setFilters((f) => ({ ...f, until: e.target.value }))}
          />
        </label>
        {hasActiveFilters && (
          <button
            className="clear-filters"
            onClick={() => setFilters({ serviceId: "", status: "", since: "", until: "" })}
          >
            Clear
          </button>
        )}
      </div>

      {loading ? (
        <div className="placeholder">LOADING…</div>
      ) : error ? (
        <div className="placeholder">ERROR: {error}</div>
      ) : incidents.length === 0 ? (
        <div className="placeholder">NO INCIDENTS MATCH THESE FILTERS</div>
      ) : (
        <div className="incident-list">
          {incidents.map((inc) => (
            <div key={inc.id} className="incident-row-wrap">
              <button
                className="incident-row"
                onClick={() => setExpandedId(expandedId === inc.id ? null : inc.id)}
                aria-expanded={expandedId === inc.id}
              >
                <span className={`status-dot ${inc.status === "open" ? (inc.severity === "critical" ? "critical" : "degraded") : "unknown"}`} />
                <span className="incident-service">{inc.service_name}</span>
                <span className="incident-type">{incidentTypeLabel(inc.type)}</span>
                <span className={`tag ${inc.severity === "critical" ? "critical" : "degraded"}`}>
                  {inc.severity.toUpperCase()}
                </span>
                <span className="incident-time">{new Date(inc.opened_at).toLocaleString()}</span>
                <span className="incident-duration">
                  {inc.status === "open" ? "ongoing" : formatDuration(inc.opened_at, inc.resolved_at)}
                </span>
              </button>
              {expandedId === inc.id && (
                <div className="incident-details">
                  <dl>
                    {Object.entries(inc.details).map(([k, v]) => (
                      <div key={k} className="detail-kv">
                        <dt>{detailKeyLabel(k)}</dt>
                        <dd>{String(v)}</dd>
                      </div>
                    ))}
                  </dl>
                  <button
                    className="view-logs-link"
                    onClick={() => {
                      const svc = services.find((s) => s.id === inc.service_id);
                      onViewLogsAround(inc.service_id, inc.opened_at, inc.resolved_at, svc?.type);
                    }}
                  >
                    → View logs around this incident
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
