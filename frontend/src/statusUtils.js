export function statusClass(status) {
  switch (status) {
    case "healthy": return "ok";
    case "degraded": return "degraded";
    case "critical": return "critical";
    default: return "unknown";
  }
}

export function statusLabel(status) {
  switch (status) {
    case "healthy": return "OK";
    case "degraded": return "WARN";
    case "critical": return "CRIT";
    default: return "?";
  }
}
