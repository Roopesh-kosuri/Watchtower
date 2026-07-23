import { useEffect, useState } from "react";
import { api } from "../api.js";
import { statusClass, statusLabel } from "../statusUtils.js";
import LatencyChart from "./LatencyChart.jsx";

const MAX_POINTS = 500;

export default function ServiceDetail({ service, livePoint }) {
  const [points, setPoints] = useState([]);
  const [baseline, setBaseline] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    if (service.type !== "http") {
      setPoints([]);
      setLoading(false);
      return;
    }

    Promise.all([
      api.getMetrics(service.id, { limit: MAX_POINTS }),
      api.getBaseline(service.id),
    ])
      .then(([metricsData, baselineData]) => {
        if (cancelled) return;
        setPoints(metricsData);
        setBaseline(baselineData);
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err.message);
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [service.id, service.type]);

  useEffect(() => {
    if (!livePoint) return;
    setPoints((prev) => {
      const next = [
        ...prev,
        {
          timestamp: new Date().toISOString(),
          latency_ms: livePoint.latency_ms,
          success: livePoint.success,
          status_code: livePoint.status_code,
        },
      ];
      return next.length > MAX_POINTS ? next.slice(next.length - MAX_POINTS) : next;
    });
  }, [livePoint]);

  const latencyBaseline = baseline.find((b) => b.metric_type === "latency");

  const successCount = points.filter((p) => p.success).length;
  const errorRateNum = points.length ? ((points.length - successCount) / points.length) * 100 : null;
  const errorRatePct = errorRateNum != null ? errorRateNum.toFixed(1) : null;

  return (
    <>
      <div className="detail-header">
        <h1>{service.name}</h1>
        <span className={`tag ${statusClass(service.status)}`}>{statusLabel(service.status)}</span>
      </div>
      <div className="detail-meta">
        <span>type: {service.type}</span>
        {service.type === "http" && (
          <span>
            last check: {service.last_check_at ? new Date(service.last_check_at).toLocaleTimeString() : "—"}
          </span>
        )}
        <span>open incidents: {service.open_incident_count}</span>
      </div>

      {service.type === "http" ? (
        <>
          <div className="stat-row">
            <div className="stat">
              <span className="label">Last latency</span>
              <span className="value">
                {service.last_latency_ms != null ? `${service.last_latency_ms.toFixed(0)}ms` : "—"}
              </span>
            </div>
            <div className="stat">
              <span className="label">Baseline mean</span>
              <span className="value">
                {latencyBaseline?.ema_mean != null ? `${latencyBaseline.ema_mean.toFixed(0)}ms` : "—"}
              </span>
            </div>
            <div className="stat">
              <span className="label">Baseline stddev</span>
              <span className="value">
                {latencyBaseline?.ema_stddev != null ? `±${latencyBaseline.ema_stddev.toFixed(0)}ms` : "—"}
              </span>
            </div>
            <div className="stat">
              <span className="label">Error rate (window)</span>
              <span className={`value ${errorRateNum > 0 ? "critical" : "ok"}`}>
                {errorRatePct != null ? `${errorRatePct}%` : "—"}
              </span>
            </div>
          </div>

          <div className="panel">
            <h2>Latency — last {points.length} checks</h2>
            {loading ? (
              <div className="placeholder">LOADING…</div>
            ) : error ? (
              <div className="placeholder">ERROR: {error}</div>
            ) : (
              <>
                <LatencyChart points={points} baseline={latencyBaseline} />
                <div className="chart-legend">
                  <span><span className="swatch" style={{ background: "var(--ok)" }} /> latency</span>
                  <span><span className="swatch" style={{ background: "var(--accent-dim)" }} /> baseline mean ± stddev</span>
                  <span>
                    <span
                      className="swatch"
                      style={{ background: "var(--crit)", borderRadius: "50%", width: 6, height: 6 }}
                    />{" "}
                    failed check
                  </span>
                </div>
              </>
            )}
          </div>
        </>
      ) : (
        <div className="panel">
          <h2>Log source</h2>
          <div className="placeholder">
            Latency charts apply to HTTP services only.
            <br />
            Log search &amp; incident timeline arrive in the next phase.
          </div>
        </div>
      )}
    </>
  );
}
