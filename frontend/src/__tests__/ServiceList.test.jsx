import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ServiceList from "../components/ServiceList.jsx";

const services = [
  { id: 1, name: "healthy-svc", type: "http", status: "healthy", open_incident_count: 0 },
  { id: 2, name: "flaky-svc", type: "http", status: "critical", open_incident_count: 2 },
  { id: 3, name: "app-logs", type: "log", status: "healthy", open_incident_count: 0 },
];

describe("ServiceList", () => {
  it("shows the configured-nothing empty state when there are no services", () => {
    render(<ServiceList services={[]} selectedId={null} onSelect={() => {}} />);
    expect(screen.getByText(/NO SERVICES CONFIGURED/)).toBeInTheDocument();
  });

  it("renders one row per service with its name and type", () => {
    render(<ServiceList services={services} selectedId={null} onSelect={() => {}} />);
    expect(screen.getByText("healthy-svc")).toBeInTheDocument();
    expect(screen.getByText("flaky-svc")).toBeInTheDocument();
    expect(screen.getByText("app-logs")).toBeInTheDocument();
  });

  it("shows the open incident count only for services that actually have open incidents", () => {
    render(<ServiceList services={services} selectedId={null} onSelect={() => {}} />);
    // flaky-svc has 2 open incidents -> badge shown
    expect(screen.getByText("2")).toBeInTheDocument();
    // healthy-svc and app-logs have 0 -> no badge text "0" anywhere (would be misleading noise)
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("calls onSelect with the correct service id when a row is clicked", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<ServiceList services={services} selectedId={null} onSelect={onSelect} />);
    await user.click(screen.getByText("flaky-svc"));
    expect(onSelect).toHaveBeenCalledWith(2);
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("marks the currently-selected row distinctly from the others", () => {
    const { container } = render(<ServiceList services={services} selectedId={2} onSelect={() => {}} />);
    const rows = container.querySelectorAll(".service-row");
    const selectedRows = container.querySelectorAll(".service-row.selected");
    expect(selectedRows).toHaveLength(1);
    expect(selectedRows[0].textContent).toContain("flaky-svc");
    expect(rows).toHaveLength(3);
  });
});
