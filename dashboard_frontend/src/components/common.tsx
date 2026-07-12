import type { ReactNode } from "react";
import type { Metric, Row, Tone } from "../types";
import { chartPoints, columnsFor, dateTime, firstValue } from "../utils/data";
import { formatValue, humanize } from "../utils/format";
import { relativeTime, useNow } from "../utils/hooks";
import { toneClass, toneGlyph } from "../viewModel";

/** Self-ticking relative time ("Ns ago") that re-renders only itself. */
export function RelativeTime({ fromMs }: { fromMs: number | null }) {
  const now = useNow(1000);
  return <>{relativeTime(fromMs, now.getTime())}</>;
}

export function ShellMessage({ title, copy, tone = "neutral" }: { title: string; copy: string; tone?: Tone }) {
  return (
    <main className="center-shell">
      <article className={`shell-card ${toneClass(tone)}`}>
        <span className="eyebrow">Maestro</span>
        <h1>{title}</h1>
        <p>{copy}</p>
      </article>
    </main>
  );
}

export function TerminalButton({
  children,
  disabled,
  onClick,
  variant = "default",
}: {
  children: ReactNode;
  disabled?: boolean;
  onClick?: () => void;
  variant?: "default" | "primary" | "danger";
}) {
  return (
    <button className={`terminal-button ${variant}`} disabled={disabled} type="button" onClick={onClick}>
      {children}
    </button>
  );
}

export function StatusPill({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  return (
    <span className={`status-pill ${toneClass(tone)}`}>
      {tone !== "neutral" && <i className="tone-glyph" aria-hidden="true">{toneGlyph(tone)}</i>}
      {children}
    </span>
  );
}



export function Panel({
  title,
  aside,
  children,
  className = "",
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`terminal-panel ${className}`}>
      <header className="panel-head">
        <span>{title}</span>
        {aside}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}

export function MetricRows({ metrics }: { metrics: Metric[] }) {
  return (
    <div className="metric-rows">
      {metrics.map((metric) => (
        <div className="metric-row" key={metric.label}>
          <b>{metric.label}</b>
          <span className={toneClass(metric.tone)}>
            {metric.tone && metric.tone !== "neutral" && (
              <i className="tone-glyph" aria-hidden="true">{toneGlyph(metric.tone)}</i>
            )}
            {formatValue(metric.value)}
          </span>
        </div>
      ))}
    </div>
  );
}

function isNumericColumn(rows: Row[], column: string): boolean {
  let numeric = 0;
  let populated = 0;
  for (const row of rows) {
    const value = row[column];
    if (value === null || value === undefined || value === "") {
      continue;
    }
    populated += 1;
    const text = typeof value === "string" ? value.trim() : "";
    const isNumericLike =
      typeof value === "number" ||
      (text !== "" && Number.isFinite(Number(text.endsWith("%") ? text.slice(0, -1) : text)));
    if (isNumericLike) {
      numeric += 1;
    }
  }
  return populated > 0 && numeric / populated >= 0.6;
}

export function CompactTable({
  columns,
  dense = false,
  limit = 8,
  rows,
}: {
  columns?: string[];
  dense?: boolean;
  limit?: number;
  rows: Row[];
}) {
  const limitedRows = rows.slice(0, limit);
  const tableColumns = columns || columnsFor(limitedRows).slice(0, 6);
  if (!limitedRows.length) {
    return <p className="muted-copy">No persisted rows are available.</p>;
  }
  const numericColumns = new Set(tableColumns.filter((column) => isNumericColumn(limitedRows, column)));
  return (
    <div className="table-scroll">
      <table className={dense ? "terminal-table dense" : "terminal-table"}>
        <thead>
          <tr>
            {tableColumns.map((column) => (
              <th className={numericColumns.has(column) ? "num" : undefined} key={column}>{humanize(column)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {limitedRows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {tableColumns.map((column) => {
                const text = formatValue(row[column]);
                return (
                  <td className={numericColumns.has(column) ? "num" : undefined} key={column} title={text}>{text}</td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function markerPoints(rows: Row[], points: ReturnType<typeof chartPoints>, xKey: string, yKey: string, markers: Row[]) {
  const plottedRows = rows.slice().reverse().filter((row) => Number.isFinite(Number(row[yKey])));
  const rowTimes = plottedRows.map((row) => dateTime(row[xKey]));
  if (!markers.length || !points.length || rowTimes.every((time) => !Number.isFinite(time))) {
    return [];
  }
  return markers
    .map((marker) => {
      const markerTime = dateTime(firstValue(marker, ["effective_at", "created_at", "as_of", "timestamp", xKey]));
      if (!Number.isFinite(markerTime)) return null;
      let nearestIndex = 0;
      let nearestDistance = Number.POSITIVE_INFINITY;
      rowTimes.forEach((rowTime, index) => {
        if (!Number.isFinite(rowTime)) return;
        const distance = Math.abs(rowTime - markerTime);
        if (distance < nearestDistance) {
          nearestDistance = distance;
          nearestIndex = index;
        }
      });
      const point = points[nearestIndex];
      return point ? { ...point, marker } : null;
    })
    .filter((p): p is ReturnType<typeof chartPoints>[number] & { marker: Row } => Boolean(p));
}

export function TerminalChart({
  title,
  rows,
  xKey = "created_at",
  yKey,
  markers = [],
}: {
  title: string;
  rows: Row[];
  xKey?: string;
  yKey: string;
  markers?: Row[];
}) {
  const points = chartPoints(rows, yKey);
  if (points.length < 2) {
    return (
      <div className="terminal-chart empty">
        <div className="chart-title">{title}</div>
        <p className="muted-copy">Not enough numeric history to draw this chart.</p>
      </div>
    );
  }
  const linePath = points.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`).join(" ");
  const areaPath = `${linePath} L ${points[points.length - 1].x} 200 L ${points[0].x} 200 Z`;
  const maxVal = Math.max(...points.map((point) => point.raw));
  const minVal = Math.min(...points.map((point) => point.raw));
  return (
    <div className="terminal-chart">
      <div className="chart-title">
        <span>{title}</span>
        <small>{formatValue(rows[0]?.created_at || rows[0]?.as_of || rows[0]?.timestamp)}</small>
      </div>
      <svg viewBox="0 0 640 220" role="img" aria-label={title}>
        <defs>
          <linearGradient id={`fill-${title.replace(/\W+/g, "-")}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--green)" stopOpacity="0.24" />
            <stop offset="100%" stopColor="var(--green)" stopOpacity="0" />
          </linearGradient>
        </defs>
        <path className="chart-grid" d="M 28 42 H 612 M 28 95 H 612 M 28 148 H 612 M 28 200 H 612" />
        <text className="chart-label" x="24" y="42" textAnchor="end">{formatValue(maxVal)}</text>
        <text className="chart-label" x="24" y="204" textAnchor="end">{formatValue(minVal)}</text>
        <path className="chart-area" d={areaPath} fill={`url(#fill-${title.replace(/\W+/g, "-")})`} />
        <path className="chart-line-shadow" d={linePath} />
        <path className="chart-line" d={linePath} />
        {markerPoints(rows, points, xKey, yKey, markers).slice(0, 12).map((point, index) => (
          <g className="cash-flow-marker" key={index}>
            <line x1={point.x} x2={point.x} y1="44" y2="200" />
            <circle cx={point.x} cy={point.y} r="4.5" />
            <title>{formatValue(point.marker.amount ?? point.marker.value)}</title>
          </g>
        ))}
        <circle className="chart-dot" cx={points[points.length - 1].x} cy={points[points.length - 1].y} r="5" />
      </svg>
    </div>
  );
}


export function Segmented<T extends string>({ values, value, onChange }: { values: readonly T[]; value: T; onChange: (value: T) => void }) {
  return (
    <div className="segmented">
      {values.map((item) => (
        <button className={item === value ? "active" : ""} key={item} type="button" onClick={() => onChange(item)}>
          {item}
        </button>
      ))}
    </div>
  );
}


export function SummaryPill({ label, value }: { label: string; value: string }) {
  return (
    <div className="summary-pill">
      <b>{label}</b>
      <span>{value}</span>
    </div>
  );
}

export type PieSlice = { label: string; value: number };

const DONUT_PALETTE = ["var(--cyan)", "var(--green)", "var(--amber)", "var(--blue)", "var(--violet)", "var(--red)"];

function polarPoint(cx: number, cy: number, r: number, angleDeg: number) {
  const angle = ((angleDeg - 90) * Math.PI) / 180;
  return { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) };
}

function donutSlicePath(cx: number, cy: number, outerR: number, innerR: number, startAngle: number, endAngle: number) {
  const clampedEnd = Math.min(endAngle, startAngle + 359.99);
  const outerStart = polarPoint(cx, cy, outerR, startAngle);
  const outerEnd = polarPoint(cx, cy, outerR, clampedEnd);
  const innerEnd = polarPoint(cx, cy, innerR, clampedEnd);
  const innerStart = polarPoint(cx, cy, innerR, startAngle);
  const largeArc = clampedEnd - startAngle > 180 ? 1 : 0;
  return [
    `M ${outerStart.x} ${outerStart.y}`,
    `A ${outerR} ${outerR} 0 ${largeArc} 1 ${outerEnd.x} ${outerEnd.y}`,
    `L ${innerEnd.x} ${innerEnd.y}`,
    `A ${innerR} ${innerR} 0 ${largeArc} 0 ${innerStart.x} ${innerStart.y}`,
    "Z",
  ].join(" ");
}

/** Compact donut chart with a text legend; used for account/app proportion breakdowns. */
export function DonutChart({
  centerLabel,
  centerValue,
  size = 108,
  slices,
  thickness = 20,
}: {
  centerLabel?: string;
  centerValue?: string;
  size?: number;
  slices: PieSlice[];
  thickness?: number;
}) {
  const total = slices.reduce((sum, slice) => sum + Math.max(0, slice.value), 0);
  const cx = size / 2;
  const cy = size / 2;
  const outerR = size / 2 - 2;
  const innerR = outerR - thickness;
  let angle = 0;
  const arcs = total > 0
    ? slices
        .filter((slice) => slice.value > 0)
        .map((slice, index) => {
          const span = (slice.value / total) * 360;
          const path = donutSlicePath(cx, cy, outerR, innerR, angle, angle + span);
          angle += span;
          return { slice, path, color: DONUT_PALETTE[index % DONUT_PALETTE.length] };
        })
    : [];

  return (
    <div className="donut-chart">
      <svg viewBox={`0 0 ${size} ${size}`} role="img" aria-label={centerLabel || "Proportion breakdown"}>
        {arcs.length === 0 ? (
          <circle className="donut-empty" cx={cx} cy={cy} r={(outerR + innerR) / 2} />
        ) : (
          arcs.map(({ color, path, slice }) => (
            <path className="donut-slice" d={path} fill={color} key={slice.label}>
              <title>{`${slice.label}: ${formatValue(slice.value)} (${((slice.value / total) * 100).toFixed(1)}%)`}</title>
            </path>
          ))
        )}
        {centerValue && <text className="donut-center-value" textAnchor="middle" x={cx} y={cy - 2}>{centerValue}</text>}
        {centerLabel && <text className="donut-center-label" textAnchor="middle" x={cx} y={cy + 13}>{centerLabel}</text>}
      </svg>
      <ul className="donut-legend">
        {arcs.length === 0 && <li className="donut-legend-empty">No data</li>}
        {arcs.map(({ color, slice }) => (
          <li key={slice.label}>
            <i style={{ background: color }} />
            <span title={slice.label}>{slice.label}</span>
            <b>{((slice.value / total) * 100).toFixed(0)}%</b>
          </li>
        ))}
      </ul>
    </div>
  );
}
