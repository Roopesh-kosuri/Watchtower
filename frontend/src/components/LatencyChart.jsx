const WIDTH = 640;
const HEIGHT = 200;
const PADDING = { top: 10, right: 10, bottom: 24, left: 46 };

export default function LatencyChart({ points, baseline }) {
  if (!points || points.length === 0) {
    return <div className="placeholder">NO DATA YET — waiting for first check</div>;
  }

  const plotW = WIDTH - PADDING.left - PADDING.right;
  const plotH = HEIGHT - PADDING.top - PADDING.bottom;

  const values = points.map((p) => p.latency_ms).filter((v) => v != null);
  const dataMax = values.length ? Math.max(...values) : 1;
  const baselineMax =
    baseline?.ema_mean != null && baseline?.ema_stddev != null
      ? baseline.ema_mean + baseline.ema_stddev * 3
      : 0;
  const yMax = Math.max(dataMax, baselineMax, 1) * 1.15;

  const x = (i) => PADDING.left + (i / Math.max(points.length - 1, 1)) * plotW;
  const y = (v) => PADDING.top + plotH - (Math.min(Math.max(v, 0), yMax) / yMax) * plotH;

  const linePath = points
    .map((p, i) => {
      const v = p.latency_ms == null ? 0 : p.latency_ms;
      return `${i === 0 ? "M" : "L"} ${x(i).toFixed(1)} ${y(v).toFixed(1)}`;
    })
    .join(" ");

  const yTicks = [0, yMax / 2, yMax];

  return (
    <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} width="100%" height={HEIGHT} role="img" aria-label="Latency over time">
      {baseline?.ema_mean != null && baseline?.ema_stddev != null && (
        <rect
          x={PADDING.left}
          y={y(baseline.ema_mean + baseline.ema_stddev)}
          width={plotW}
          height={Math.max(
            y(baseline.ema_mean - baseline.ema_stddev) - y(baseline.ema_mean + baseline.ema_stddev),
            0
          )}
          fill="var(--accent)"
          opacity="0.08"
        />
      )}
      {baseline?.ema_mean != null && (
        <line
          x1={PADDING.left}
          x2={WIDTH - PADDING.right}
          y1={y(baseline.ema_mean)}
          y2={y(baseline.ema_mean)}
          stroke="var(--accent-dim)"
          strokeDasharray="3 3"
          strokeWidth="1"
        />
      )}

      {yTicks.map((t, i) => (
        <g key={i}>
          <line
            x1={PADDING.left} x2={WIDTH - PADDING.right}
            y1={y(t)} y2={y(t)}
            stroke="var(--border)" strokeWidth="1"
          />
          <text
            x={PADDING.left - 6} y={y(t) + 3}
            textAnchor="end" fontSize="9" fill="var(--text-faint)"
          >
            {Math.round(t)}
          </text>
        </g>
      ))}

      <path d={linePath} fill="none" stroke="var(--ok)" strokeWidth="1.5" />

      {points.map((p, i) =>
        !p.success ? (
          <circle key={i} cx={x(i)} cy={y(p.latency_ms ?? 0)} r="2.5" fill="var(--crit)" />
        ) : null
      )}
    </svg>
  );
}
