import type { Row } from "../../types";
import { chartPoints, dateTime, firstValue } from "../../utils/data";
import { formatValue } from "../../utils/format";
import { Panel } from "../ui/Panel";

function markerPoints(rows: Row[], points: ReturnType<typeof chartPoints>, xKey: string, yKey: string, markers: Row[]) {
  const plottedRows = rows
    .slice()
    .reverse()
    .filter((row) => Number.isFinite(Number(row[yKey])));
  const rowTimes = plottedRows.map((row) => dateTime(row[xKey]));
  if (!markers.length || !points.length || rowTimes.every((time) => !Number.isFinite(time))) {
    return [];
  }
  return markers
    .map((marker) => {
      const markerTime = dateTime(firstValue(marker, ["effective_at", "created_at", "as_of", "timestamp", xKey]));
      if (!Number.isFinite(markerTime)) {
        return null;
      }
      let nearestIndex = 0;
      let nearestDistance = Number.POSITIVE_INFINITY;
      rowTimes.forEach((rowTime, index) => {
        if (!Number.isFinite(rowTime)) {
          return;
        }
        const distance = Math.abs(rowTime - markerTime);
        if (distance < nearestDistance) {
          nearestDistance = distance;
          nearestIndex = index;
        }
      });
      const point = points[nearestIndex];
      if (!point) {
        return null;
      }
      return { ...point, marker };
    })
    .filter((point): point is ReturnType<typeof chartPoints>[number] & { marker: Row } => Boolean(point));
}

export function LineChart({
  title,
  rows,
  xKey,
  yKey,
  markers = [],
}: {
  title: string;
  rows: Row[];
  xKey: string;
  yKey: string;
  markers?: Row[];
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
  const cashFlowPoints = markerPoints(rows, points, xKey, yKey, markers);
  const gradientId = `grad-${title.replace(/\s+/g, "-")}`;

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
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--primary)" stopOpacity="0.18" />
            <stop offset="100%" stopColor="var(--primary)" stopOpacity="0.01" />
          </linearGradient>
        </defs>
        <path className="chart-grid" d="M 28 42 H 612 M 28 95 H 612 M 28 148 H 612 M 28 200 H 612" />
        <text x="24" y="40" className="chart-label" textAnchor="end">{formatValue(maxVal)}</text>
        <text x="24" y="204" className="chart-label" textAnchor="end">{formatValue(minVal)}</text>
        <path className="chart-area" d={areaPath} fill={`url(#${gradientId})`} />
        <path className="chart-line-shadow" d={linePath} />
        <path className="chart-line" d={linePath} />
        {cashFlowPoints.map((point, index) => (
          <g className="cash-flow-marker" key={`${point.x}-${index}`}>
            <line x1={point.x} x2={point.x} y1="44" y2="200" />
            <circle cx={point.x} cy={point.y} r="6" />
            <title>{formatValue(point.marker.amount)}</title>
          </g>
        ))}
        <circle cx={lastPoint.x} cy={lastPoint.y} r="5" className="chart-dot" />
        <circle cx={lastPoint.x} cy={lastPoint.y} r="2.5" className="chart-dot-inner" />
        <circle cx={firstPoint.x} cy={firstPoint.y} r="3.5" className="chart-dot-start" />
      </svg>
    </article>
  );
}
