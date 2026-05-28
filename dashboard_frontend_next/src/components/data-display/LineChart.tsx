import type { Row } from "../../types";
import { chartPoints } from "../../utils/data";
import { formatValue } from "../../utils/format";
import { Panel } from "../ui/Panel";

export function LineChart({
  title,
  rows,
  xKey,
  yKey,
}: {
  title: string;
  rows: Row[];
  xKey: string;
  yKey: string;
}) {
  const points = chartPoints(rows, yKey);
  if (points.length < 2) {
    return (
      <Panel title={title} eyebrow="Chart">
        <p className="muted-copy">Not enough numeric history to draw this chart.</p>
      </Panel>
    );
  }
  const linePath = points
    .map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`)
    .join(" ");
  const areaPath = `${linePath} L ${points[points.length - 1].x} 200 L ${points[0].x} 200 Z`;
  const lastPoint = points[points.length - 1];
  const firstPoint = points[0];
  const minVal = Math.min(...points.map((p) => p.raw));
  const maxVal = Math.max(...points.map((p) => p.raw));

  return (
    <article className="chart-panel">
      <div className="chart-title">
        <div>
          <span className="eyebrow">Chart</span>
          <h2>{title}</h2>
        </div>
        <span>
          {formatValue(rows[rows.length - 1]?.[xKey])} to {formatValue(rows[0]?.[xKey])}
        </span>
      </div>
      <svg viewBox="0 0 640 220" role="img" aria-label={title}>
        <defs>
          <linearGradient id={`grad-${title.replace(/\s+/g, "-")}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--primary)" stopOpacity="0.18" />
            <stop offset="100%" stopColor="var(--primary)" stopOpacity="0.01" />
          </linearGradient>
        </defs>
        <path className="chart-grid" d="M 28 42 H 612 M 28 95 H 612 M 28 148 H 612 M 28 200 H 612" />
        {/* Y-axis labels */}
        <text x="24" y="40" className="chart-label" textAnchor="end">{formatValue(maxVal)}</text>
        <text x="24" y="204" className="chart-label" textAnchor="end">{formatValue(minVal)}</text>
        {/* Area fill */}
        <path className="chart-area" d={areaPath} fill={`url(#grad-${title.replace(/\s+/g, "-")})`} />
        {/* Line shadow and line */}
        <path className="chart-line-shadow" d={linePath} />
        <path className="chart-line" d={linePath} />
        {/* End point marker */}
        <circle cx={lastPoint.x} cy={lastPoint.y} r="5" className="chart-dot" />
        <circle cx={lastPoint.x} cy={lastPoint.y} r="2.5" className="chart-dot-inner" />
        {/* Start point marker */}
        <circle cx={firstPoint.x} cy={firstPoint.y} r="3.5" className="chart-dot-start" />
      </svg>
    </article>
  );
}
