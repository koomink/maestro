import { type CSSProperties, type MouseEvent as ReactMouseEvent, type ReactNode, useRef, useState } from "react";
import type { Metric, Row, Tone } from "../types";
import { chartPoints, columnsFor, dateTime, firstValue } from "../utils/data";
import { formatCompact, formatPercent, formatValue, humanize } from "../utils/format";
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
  emptyLabel = "No persisted rows are available.",
  limit = 8,
  onRowClick,
  rows,
}: {
  columns?: string[];
  dense?: boolean;
  emptyLabel?: string;
  limit?: number;
  onRowClick?: (row: Row) => void;
  rows: Row[];
}) {
  const limitedRows = rows.slice(0, limit);
  const tableColumns = columns || columnsFor(limitedRows).slice(0, 6);
  if (!limitedRows.length) {
    return <p className="muted-copy">{emptyLabel}</p>;
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
            <tr
              className={onRowClick ? "row-clickable" : undefined}
              key={rowIndex}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            >
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
      {rows.length > limitedRows.length && (
        <p className="table-note">Showing {limitedRows.length} of {rows.length} rows</p>
      )}
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

/** Axis date label in KST ("07-12"). */
function axisDate(time: number): string {
  if (!Number.isFinite(time)) {
    return "";
  }
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Seoul", month: "2-digit", day: "2-digit" }).format(new Date(time));
}

function axisDateTime(time: number): string {
  if (!Number.isFinite(time)) {
    return "";
  }
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(time));
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
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const points = chartPoints(rows, yKey);
  if (points.length < 2) {
    return (
      <div className="terminal-chart empty">
        <div className="chart-title">{title}</div>
        <p className="muted-copy">Not enough numeric history to draw this chart.</p>
      </div>
    );
  }
  // Times parallel to `points`: same ordering/filter as chartPoints.
  const pointTimes = rows
    .slice()
    .reverse()
    .filter((row) => Number.isFinite(Number(row[yKey])))
    .map((row) => dateTime(firstValue(row, [xKey, "as_of", "timestamp", "date"])));
  const linePath = points.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`).join(" ");
  const maxVal = Math.max(...points.map((point) => point.raw));
  const minVal = Math.min(...points.map((point) => point.raw));
  const firstVal = points[0].raw;
  const lastVal = points[points.length - 1].raw;
  const periodReturn = firstVal !== 0 ? lastVal / firstVal - 1 : null;
  const up = lastVal >= firstVal;
  // The y axis spans only min..max, so a small move fills the plot. State what
  // the full plot height is actually worth, or the amplitude reads as drama.
  const spanPct = minVal !== 0 ? (maxVal - minVal) / Math.abs(minVal) : null;
  const gridYs = [42, 95, 148, 200];
  const valueAtY = (y: number) => minVal + ((200 - y) / 158) * (maxVal - minVal);
  const xLabelIndexes = Array.from(
    new Set([0, Math.floor((points.length - 1) / 2), points.length - 1]),
  ).filter((index) => Number.isFinite(pointTimes[index]));
  const hovered = hoverIndex != null ? points[hoverIndex] : null;
  const hoveredTime = hoverIndex != null ? pointTimes[hoverIndex] : Number.NaN;

  function onMouseMove(event: ReactMouseEvent<SVGSVGElement>) {
    const svg = svgRef.current;
    if (!svg) {
      return;
    }
    const rect = svg.getBoundingClientRect();
    const viewX = ((event.clientX - rect.left) / rect.width) * 640;
    let nearest = 0;
    let nearestDistance = Number.POSITIVE_INFINITY;
    points.forEach((point, index) => {
      const distance = Math.abs(point.x - viewX);
      if (distance < nearestDistance) {
        nearestDistance = distance;
        nearest = index;
      }
    });
    setHoverIndex(nearest);
  }

  const tooltipFlip = hovered != null && hovered.x > 470;
  const tooltipX = hovered == null ? 0 : tooltipFlip ? hovered.x - 148 : hovered.x + 10;
  const tooltipY = hovered == null ? 0 : Math.min(158, Math.max(46, hovered.y - 44));

  return (
    <div
      className="terminal-chart"
      style={{ "--chart-color": up ? "var(--green)" : "var(--red)" } as CSSProperties}
    >
      <div className="chart-title">
        <span>{title}</span>
        <span className="chart-title-meta">
          {spanPct != null && (
            <span className="chart-scale">full height {(spanPct * 100).toFixed(1)}%</span>
          )}
          {periodReturn != null && (
            <span className={up ? "chart-return tone-success" : "chart-return tone-danger"}>
              {formatPercent(periodReturn)}
            </span>
          )}
          <small>
            {axisDate(pointTimes[0])} → {axisDate(pointTimes[points.length - 1])}
          </small>
        </span>
      </div>
      <svg
        ref={svgRef}
        viewBox="0 0 640 236"
        role="img"
        aria-label={title}
        onMouseMove={onMouseMove}
        onMouseLeave={() => setHoverIndex(null)}
      >
        <path className="chart-grid" d="M 28 42 H 612 M 28 95 H 612 M 28 148 H 612 M 28 200 H 612" />
        {gridYs.map((y) => (
          <text className="chart-label" key={y} x="32" y={y - 4} textAnchor="start">
            {formatCompact(valueAtY(y))}
          </text>
        ))}
        {xLabelIndexes.map((index) => (
          <text
            className="chart-label"
            key={index}
            x={points[index].x}
            y="216"
            textAnchor={index === 0 ? "start" : index === points.length - 1 ? "end" : "middle"}
          >
            {axisDate(pointTimes[index])}
          </text>
        ))}
        <line className="chart-baseline" x1="28" x2="612" y1={points[0].y} y2={points[0].y} />
        <text className="chart-baseline-label" x="612" y={Math.max(52, points[0].y - 6)} textAnchor="end">
          open · {formatCompact(firstVal)}
        </text>
        <path className="chart-line" d={linePath} />
        {markerPoints(rows, points, xKey, yKey, markers).slice(0, 12).map((point, index) => (
          <g className="cash-flow-marker" key={index}>
            <line x1={point.x} x2={point.x} y1="44" y2="200" />
            <circle cx={point.x} cy={point.y} r="4.5" />
            <title>{formatValue(point.marker.amount ?? point.marker.value)}</title>
          </g>
        ))}
        <circle className="chart-dot" cx={points[points.length - 1].x} cy={points[points.length - 1].y} r="3.5" />
        {hovered && (
          <g className="chart-hover">
            <line className="chart-crosshair" x1={hovered.x} x2={hovered.x} y1="42" y2="200" />
            <circle className="chart-hover-dot" cx={hovered.x} cy={hovered.y} r="4" />
            <g transform={`translate(${tooltipX} ${tooltipY})`}>
              <rect className="chart-tooltip-box" width="138" height="36" rx="4" />
              <text className="chart-tooltip-date" x="8" y="14">{axisDateTime(hoveredTime)}</text>
              <text className="chart-tooltip-value" x="8" y="29">{formatValue(hovered.raw)}</text>
            </g>
          </g>
        )}
      </svg>
    </div>
  );
}

export type CompareSeriesInput = {
  label: string;
  color: string;
  rows: Row[];
  yKey: string;
};

/**
 * Multi-series overlay chart with every series rebased to 100 at its first
 * plotted point, so apps with different book sizes can be compared directly.
 */
export function CompareChart({ series, title }: { series: CompareSeriesInput[]; title: string }) {
  const lines = series
    .map((item) => {
      const values = item.rows
        .slice()
        .reverse()
        .map((row) => Number(row[item.yKey]))
        .filter((value) => Number.isFinite(value));
      return { label: item.label, color: item.color, values };
    })
    .filter((line) => line.values.length >= 2 && line.values[0] !== 0)
    .map((line) => ({
      ...line,
      normalized: line.values.map((value) => (value / line.values[0]) * 100),
    }));
  if (!lines.length) {
    return (
      <div className="terminal-chart empty">
        <div className="chart-title">{title}</div>
        <p className="muted-copy">Not enough numeric history to compare apps.</p>
      </div>
    );
  }
  const allValues = lines.flatMap((line) => line.normalized);
  const min = Math.min(...allValues);
  const max = Math.max(...allValues);
  const range = max - min || 1;
  const toY = (value: number) => 200 - ((value - min) / range) * 158;
  const gridYs = [42, 95, 148, 200];
  const valueAtY = (y: number) => min + ((200 - y) / 158) * range;
  return (
    <div className="terminal-chart compare-chart">
      <div className="chart-title">
        <span>{title}</span>
        <small>rebased to 100</small>
      </div>
      <svg viewBox="0 0 640 220" role="img" aria-label={title}>
        <path className="chart-grid" d="M 28 42 H 612 M 28 95 H 612 M 28 148 H 612 M 28 200 H 612" />
        {gridYs.map((y) => (
          <text className="chart-label" key={y} x="32" y={y - 4} textAnchor="start">
            {valueAtY(y).toFixed(1)}
          </text>
        ))}
        {lines.map((line) => {
          const step = 584 / Math.max(line.normalized.length - 1, 1);
          const path = line.normalized
            .map((value, index) => `${index === 0 ? "M" : "L"} ${28 + index * step} ${toY(value)}`)
            .join(" ");
          return <path className="compare-line" d={path} key={line.label} style={{ stroke: line.color }} />;
        })}
      </svg>
      <ul className="compare-legend">
        {lines.map((line) => {
          const change = line.normalized[line.normalized.length - 1] / 100 - 1;
          return (
            <li key={line.label}>
              <i style={{ background: line.color }} />
              <span>{line.label}</span>
              <b className={change >= 0 ? "tone-success" : "tone-danger"}>{formatPercent(change)}</b>
            </li>
          );
        })}
      </ul>
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

/**
 * Categorical ramp for proportion breakdowns. Deliberately shares no hue with
 * the status palette — reusing --green/--amber/--red here made an account
 * render amber and read as a warning when it was only the second slice.
 */
const DONUT_PALETTE = [
  "var(--cat-1)",
  "var(--cat-2)",
  "var(--cat-3)",
  "var(--cat-4)",
  "var(--cat-5)",
  "var(--cat-6)",
];

/** Stable series color by index — shared by donuts and compare charts. */
export function seriesColor(index: number): string {
  return DONUT_PALETTE[index % DONUT_PALETTE.length];
}

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
