import type { ReactNode } from "react";
import type { Metric, Row, Tone } from "../types";
import { chartPoints, columnsFor, firstValue } from "../utils/data";
import { formatValue, humanize } from "../utils/format";
import { toneClass } from "../viewModel";

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
  return <span className={`status-pill ${toneClass(tone)}`}>{children}</span>;
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
          <span className={toneClass(metric.tone)}>{formatValue(metric.value)}</span>
        </div>
      ))}
    </div>
  );
}

export function CompactTable({ rows, limit = 8, columns }: { rows: Row[]; limit?: number; columns?: string[] }) {
  const limitedRows = rows.slice(0, limit);
  const tableColumns = columns || columnsFor(limitedRows).slice(0, 6);
  if (!limitedRows.length) {
    return <p className="muted-copy">No persisted rows are available.</p>;
  }
  return (
    <div className="table-scroll">
      <table className="terminal-table">
        <thead>
          <tr>
            {tableColumns.map((column) => (
              <th key={column}>{humanize(column)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {limitedRows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {tableColumns.map((column) => (
                <td key={column}>{formatValue(row[column])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function TerminalChart({
  title,
  rows,
  yKey,
  markers = [],
}: {
  title: string;
  rows: Row[];
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
        {markers.slice(0, 8).map((marker, index) => {
          const point = points[Math.min(points.length - 1, Math.floor((index / Math.max(markers.length - 1, 1)) * (points.length - 1)))];
          return (
            <g className="cash-flow-marker" key={index}>
              <line x1={point.x} x2={point.x} y1="44" y2="200" />
              <circle cx={point.x} cy={point.y} r="4.5" />
              <title>{formatValue(marker.amount ?? marker.value ?? marker.created_at)}</title>
            </g>
          );
        })}
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
