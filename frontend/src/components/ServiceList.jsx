import StatusDot from "./StatusDot.jsx";

export default function ServiceList({ services, selectedId, onSelect }) {
  if (services.length === 0) {
    return (
      <div className="empty-state">
        NO SERVICES CONFIGURED
        <br />
        Add services to config.yaml and restart the backend.
      </div>
    );
  }

  return (
    <div>
      <div className="eyebrow">Services · {services.length}</div>
      {services.map((svc) => (
        <button
          key={svc.id}
          className={`service-row${svc.id === selectedId ? " selected" : ""}`}
          onClick={() => onSelect(svc.id)}
        >
          <StatusDot status={svc.status} />
          <span className="name">{svc.name}</span>
          <span className="type-tag">{svc.type}</span>
          {svc.open_incident_count > 0 && (
            <span className="incident-count">{svc.open_incident_count}</span>
          )}
        </button>
      ))}
    </div>
  );
}
