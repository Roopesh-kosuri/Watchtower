import { statusClass } from "../statusUtils.js";

export default function StatusDot({ status }) {
  return <span className={`status-dot ${statusClass(status)}`} aria-hidden="true" />;
}
