import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import LatencyChart from "../components/LatencyChart.jsx";

describe("LatencyChart", () => {
  it("shows the empty-state message when there are no points, not a blank chart", () => {
    render(<LatencyChart points={[]} baseline={null} />);
    expect(screen.getByText(/NO DATA YET/)).toBeInTheDocument();
  });

  it("renders an SVG with a line path when points exist", () => {
    const points = [
      { timestamp: "2026-01-01T00:00:00Z", latency_ms: 100, success: true },
      { timestamp: "2026-01-01T00:00:05Z", latency_ms: 120, success: true },
      { timestamp: "2026-01-01T00:00:10Z", latency_ms: 90, success: false },
    ];
    const { container } = render(<LatencyChart points={points} baseline={null} />);
    const svg = container.querySelector("svg");
    expect(svg).toBeInTheDocument();

    const path = container.querySelector("path");
    expect(path).toBeInTheDocument();
    // The line path must actually reference all 3 points (3 command letters: M + 2 L)
    expect(path.getAttribute("d").match(/[ML]/g)).toHaveLength(3);
  });

  it("draws a red marker for a failed check, matching the failure in the data", () => {
    const points = [
      { timestamp: "t1", latency_ms: 100, success: true },
      { timestamp: "t2", latency_ms: 100, success: false },
    ];
    const { container } = render(<LatencyChart points={points} baseline={null} />);
    const circles = container.querySelectorAll("circle");
    // exactly one failure in the data -> exactly one failure marker, not zero, not two
    expect(circles).toHaveLength(1);
  });

  it("draws a baseline band and mean line when baseline data is provided", () => {
    const points = [{ timestamp: "t1", latency_ms: 100, success: true }];
    const baseline = { metric_type: "latency", ema_mean: 100, ema_stddev: 10, sample_count: 50 };
    const { container } = render(<LatencyChart points={points} baseline={baseline} />);
    const rects = container.querySelectorAll("rect");
    const dashedLines = Array.from(container.querySelectorAll("line")).filter(
      (l) => l.getAttribute("stroke-dasharray")
    );
    expect(rects.length).toBeGreaterThan(0);
    expect(dashedLines.length).toBe(1);
  });
});
