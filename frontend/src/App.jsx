import { useCallback, useEffect, useRef, useState } from "react";
import { api, useLiveEvents } from "./api.js";
import ServiceList from "./components/ServiceList.jsx";
import ServiceDetail from "./components/ServiceDetail.jsx";
import IncidentsView from "./components/IncidentsView.jsx";
import LogsView from "./components/LogsView.jsx";

function useClock() {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

export default function App() {
  const [view, setView] = useState("services"); // 'services' | 'incidents' | 'logs'
  const [services, setServices] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [livePointBuffer, setLivePointBuffer] = useState(null);
  const [incidentEventTick, setIncidentEventTick] = useState(0);
  const [lastLogEvent, setLastLogEvent] = useState(null);
  const [logsFilter, setLogsFilter] = useState(null);
  const refetchTimer = useRef(null);
  const now = useClock();

  const refetchServices = useCallback(() => {
    api
      .listServices()
      .then((data) => {
        setServices(data);
        setLoadError(null);
      })
      .catch((err) => setLoadError(err.message));
  }, []);

  useEffect(() => {
    refetchServices();
  }, [refetchServices]);

  const { connected } = useLiveEvents((event) => {
    // Any live signal could change a service's computed status (a new
    // incident, a resolution, a fresh check) -- debounce a full refetch
    // of the service list rather than hand-patching partial state, which
    // is simple and correct at this tool's scale.
    if (refetchTimer.current) clearTimeout(refetchTimer.current);
    refetchTimer.current = setTimeout(refetchServices, 300);

    if (event.event === "health_check") {
      setLivePointBuffer({ serviceId: event.service_id, point: event });
    }
    if (event.event?.startsWith("incident_")) {
      setIncidentEventTick((t) => t + 1);
    }
    if (event.event === "log_event") {
      setLastLogEvent(event);
    }
  });

  useEffect(() => {
    if (services.length > 0 && selectedId == null) {
      setSelectedId(services[0].id);
    }
  }, [services, selectedId]);

  const selectedService = services.find((s) => s.id === selectedId) || null;

  const handleViewLogsAround = useCallback((serviceId, openedAt, resolvedAt, serviceType) => {
    const opened = new Date(openedAt);
    const since = new Date(opened.getTime() - 30 * 60 * 1000).toISOString();
    const untilDate = resolvedAt
      ? new Date(new Date(resolvedAt).getTime() + 30 * 60 * 1000)
      : new Date(opened.getTime() + 2 * 60 * 60 * 1000);
    setLogsFilter({
      // Only pre-filter by service if the incident's own service is
      // actually a log source. An incident on an http service (e.g.
      // latency_drift, error_rate_spike) has no guaranteed corresponding
      // log-type service in the data model -- there's no link between
      // "payments-api" the HTTP check and "payments-api-logs" the log
      // source beyond a shared naming convention a person chose, which
      // the system can't rely on. Searching all log sources in the time
      // window is more honest than filtering to a service ID with a
      // 400 error or zero results.
      serviceId: serviceType === "log" ? serviceId : "",
      since,
      until: untilDate.toISOString(),
      nonce: Date.now(),
    });
    setView("logs");
  }, []);

  return (
    <div className="app">
      <div className="topbar">
        <div className="wordmark">
          <span className="bracket">[</span> WATCHTOWER <span className="bracket">]</span>
        </div>
        <nav className="tabs">
          <button className={view === "services" ? "active" : ""} onClick={() => setView("services")}>
            Services
          </button>
          <button className={view === "incidents" ? "active" : ""} onClick={() => setView("incidents")}>
            Incidents
          </button>
          <button className={view === "logs" ? "active" : ""} onClick={() => setView("logs")}>
            Logs
          </button>
        </nav>
        <div className="conn-indicator">
          <span className="clock">{now.toLocaleTimeString()}</span>
          <span className={`conn-dot ${connected ? "live" : "offline"}`} />
          {connected ? "LIVE" : "OFFLINE"}
        </div>
      </div>

      {view === "services" && (
        <div className="main">
          <div className={`service-list-pane${selectedId != null ? " has-selection" : ""}`}>
            {loadError ? (
              <div className="empty-state">
                CONNECTION ERROR
                <br />
                {loadError}
              </div>
            ) : (
              <ServiceList services={services} selectedId={selectedId} onSelect={setSelectedId} />
            )}
          </div>
          <div className="detail-pane">
            {selectedService && (
              <button className="back-button" onClick={() => setSelectedId(null)}>
                ← BACK
              </button>
            )}
            {selectedService ? (
              <ServiceDetail
                key={selectedService.id}
                service={selectedService}
                livePoint={livePointBuffer?.serviceId === selectedService.id ? livePointBuffer.point : null}
              />
            ) : (
              <div className="placeholder">SELECT A SERVICE</div>
            )}
          </div>
        </div>
      )}

      {view === "incidents" && (
        <IncidentsView
          services={services}
          refetchTick={incidentEventTick}
          onViewLogsAround={handleViewLogsAround}
        />
      )}

      {view === "logs" && (
        <LogsView services={services} initialFilter={logsFilter} liveEvent={lastLogEvent} />
      )}
    </div>
  );
}
