import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { fetchSnapshot, generateStrategySignal, loadSnapshot, refreshDashboardState } from "./api/snapshot";
import type { DashboardSnapshot, Metric, Row, Tone } from "./types";
import { chartPoints, columnsFor, filterByPeriod, firstValue } from "./utils/data";
import { buildDiagnosticContext } from "./utils/diagnostic";
import { formatValue, humanize } from "./utils/format";
import { toneFromValue } from "./utils/tone";
import { evidenceSummaries, trustSummary } from "./utils/trust";

const tabs = ["Portfolio", "Maestro", "Virtuoso", "Research"] as const;
const periods = ["7D", "30D", "90D", "All"] as const;

type TabName = (typeof tabs)[number];
type Period = (typeof periods)[number];
type ActionState = { busy: boolean; message: string; tone?: Tone };
type SignalActionState = ActionState & { strategyId: string };
type Strategy = DashboardSnapshot["virtuoso_apps"]["strategies"][number];

function toneClass(tone?: Tone) {
  return `tone-${String(tone || "neutral").toLowerCase()}`;
}

function appName(strategyId: string) {
  const known: Record<string, string> = {
    tranquillo: "Tranquillo",
    crescendo_us: "Crescendo",
    crescendo: "Crescendo",
    fugue: "Fugue",
  };
  return known[strategyId] || humanize(strategyId);
}

function rowValue(row: Row | undefined, keys: string[]) {
  return formatValue(firstValue(row, keys));
}

function metricValue(metrics: Metric[], label: string, fallback = "n/a") {
  return formatValue(metrics.find((metric) => metric.label === label)?.value ?? fallback);
}

function latestRow(rows: Row[]) {
  return rows[0] || {};
}

function latestStrategy(snapshot: DashboardSnapshot, strategyId: string) {
  return snapshot.virtuoso_apps.strategies.find((strategy) => strategy.strategy_id === strategyId);
}

function latestSignal(snapshot: DashboardSnapshot, strategyId: string) {
  return snapshot.virtuoso_apps.signal_freshness.strategies.find(
    (row) => row.strategy_id === strategyId,
  );
}

function strategyMetrics(strategy: Strategy): Metric[] {
  const latest = strategy.performance_snapshot?.latest || {};
  const quality = strategy.performance_snapshot?.quality?.status || "missing";
  return [
    { label: "Signal", value: latestSignalLabel(strategy), tone: toneFromValue(latestSignalLabel(strategy)) },
    { label: "Current Value", value: latest.current_value ?? strategy.summary.current_value ?? strategy.summary.book_value ?? "n/a" },
    { label: "Return", value: latest.cumulative_return ?? strategy.summary.cumulative_return ?? "n/a", tone: "success" },
    { label: "Evidence", value: quality, tone: toneFromValue(quality) },
  ];
}

function latestSignalLabel(strategy: Strategy) {
  const run = strategy.runs[0];
  if (run?.validation_ok === true) {
    return "fresh";
  }
  if (run?.validation_ok === false) {
    return "failed";
  }
  return "missing";
}

function ShellMessage({ title, copy, tone = "neutral" }: { title: string; copy: string; tone?: Tone }) {
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

function TerminalButton({
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

function StatusPill({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  return <span className={`status-pill ${toneClass(tone)}`}>{children}</span>;
}



function Panel({
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

function MetricRows({ metrics }: { metrics: Metric[] }) {
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

function CompactTable({ rows, limit = 8, columns }: { rows: Row[]; limit?: number; columns?: string[] }) {
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

function TerminalChart({
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

function TopChrome({
  activeTab,
  consoleOpen,
  displayCurrency,
  loading,
  refreshAction,
  setActiveTab,
  setConsoleOpen,
  setDisplayCurrency,
  snapshot,
  onRefresh,
}: {
  activeTab: TabName;
  consoleOpen: boolean;
  displayCurrency: "KRW" | "USD";
  loading: boolean;
  refreshAction: ActionState;
  setActiveTab: (tab: TabName) => void;
  setConsoleOpen: (open: boolean) => void;
  setDisplayCurrency: (currency: "KRW" | "USD") => void;
  snapshot: DashboardSnapshot;
  onRefresh: () => void;
}) {
  const trust = trustSummary(snapshot);
  const configFingerprint = snapshot.header.operator_config?.fingerprint || "n/a";
  const account = snapshot.investment_console.broker_account_overview.accounts[0] || {};
  const total = latestRow(snapshot.investment_console.total_portfolio_performance);
  const researchRows = snapshot.audit_trail.strategy_run_payloads.length + snapshot.virtuoso_apps.strategies.length;
  return (
    <>
      <header className="topbar">
        <div className="brand">SYMPHONY <span>&gt;</span></div>
        <nav className="nav-tabs" aria-label="Dashboard sections">
          {tabs.map((tab) => (
            <button className={tab === activeTab ? "active" : ""} key={tab} type="button" onClick={() => setActiveTab(tab)}>
              {tab}
            </button>
          ))}
        </nav>

        <div className="top-actions">
          <select value={displayCurrency} onChange={(event) => setDisplayCurrency(event.target.value as "KRW" | "USD")}>
            <option value="KRW">KRW</option>
            <option value="USD">USD</option>
          </select>
          <TerminalButton disabled={loading || refreshAction.busy} onClick={onRefresh} variant="primary">
            {refreshAction.busy ? "Refreshing" : "Refresh"}
          </TerminalButton>
          <TerminalButton onClick={() => setConsoleOpen(!consoleOpen)}>Console</TerminalButton>
          <span>{formatValue(new Date().toISOString())}</span>
          <span><i className="live-dot" />Connected</span>
        </div>
      </header>
      <section className="ticker-strip" aria-label="Dashboard status strip">
        <TickerCell label="Total NAV" value={formatValue(total.total_value ?? total.current_value ?? total.value)} delta={displayCurrency} />
        <TickerCarousel accounts={snapshot.investment_console.broker_account_overview.accounts} />
        <TickerCell label="Cash" value={metricValue(snapshot.investment_console.metrics, "Cash", "n/a")} delta="ready" />
        <TickerCell label="Freshness" value={trust.freshness} delta="signals" tone={trust.freshnessTone} />
        <TickerCell label="Recon" value={trust.reconciliation} delta="state" tone={trust.reconciliationTone} />
        <TickerCell label="Gate" value={snapshot.header.order_posture} delta={snapshot.read_only ? "locked" : "active"} tone={snapshot.read_only ? "warning" : "success"} />
        <TickerCell label="Config" value={String(configFingerprint).slice(0, 8)} delta="op" />
        <TickerCell label="Research" value={`${researchRows} rows`} delta="read model" />
      </section>
    </>
  );
}

function TickerCarousel({ accounts }: { accounts: any[] }) {
  const [currentIndex, setCurrentIndex] = useState(0);

  useEffect(() => {
    if (!accounts || accounts.length <= 1) return;
    const interval = setInterval(() => {
      setCurrentIndex((i) => (i + 1) % accounts.length);
    }, 5000);
    return () => clearInterval(interval);
  }, [accounts]);

  if (!accounts || accounts.length === 0) {
    return <TickerCell label="Account" value="synced" />;
  }

  return (
    <div className="ticker-cell carousel-wrapper" style={{ padding: 0, position: "relative" }}>
      <div 
        className="carousel-track" 
        style={{ 
          transform: `translateY(-${currentIndex * 100}%)`,
          transition: "transform 0.5s cubic-bezier(0.4, 0, 0.2, 1)",
          height: "100%"
        }}
      >
        {accounts.map((acc: any, i: number) => {
          const accountId = formatValue(acc.account_id || "Account");
          const status = formatValue(acc.status || "synced");
          const timestamp = rowValue(acc, ["created_at", "as_of"]);
          return (
            <div 
              key={acc.account_id || i} 
              className={`carousel-item ${toneClass(toneFromValue(acc.status))}`}
              title={`${accountId}: ${status} ${timestamp}`.trim()}
            >
              <b>{accountId}</b>
              <span>
                {status}
                {<small>{timestamp}</small>}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function TickerCell({ label, value, delta, tone = "neutral" }: { label: string; value: string; delta?: string; tone?: Tone }) {
  return (
    <div className={`ticker-cell ${toneClass(tone)}`} title={`${label}: ${value} ${delta || ""}`.trim()}>
      <b>{label}</b>
      <span>{value} {delta && <small>{delta}</small>}</span>
    </div>
  );
}

function PortfolioTab({ period, setPeriod, snapshot }: { period: Period; setPeriod: (period: Period) => void; snapshot: DashboardSnapshot }) {
  const investment = snapshot.investment_console;
  const rows = filterByPeriod(investment.total_portfolio_performance, period);
  const yKey = rows.some((row) => Number.isFinite(Number(row.total_value))) ? "total_value" : "current_value";
  return (
    <section className="tab-grid portfolio-grid">
      <Panel title="Market / Portfolio Pulse" aside={<StatusPill tone="success">Risk-on</StatusPill>}>
        <MetricRows metrics={[...investment.asset_summary_metrics, ...investment.metrics].slice(0, 8)} />
      </Panel>
      <Panel
        className="main-chart-panel"
        title="Portfolio Value / Return / Cash Flow"
        aside={<Segmented values={periods} value={period} onChange={setPeriod} />}
      >
        <TerminalChart title="Portfolio Value" rows={rows} yKey={yKey} markers={investment.performance_snapshot.series.total_portfolio} />
      </Panel>
      <Panel title="AI Summary" aside={<StatusPill tone="primary">Read-only</StatusPill>}>
        <div className="ai-copy">
          <h3>Operator Brief</h3>
          <p>{snapshot.system_verdict.copy}</p>
        </div>
        <MetricRows metrics={snapshot.system_verdict.capital_summary.slice(0, 5)} />
      </Panel>
      <Panel className="span-2" title="Account / App Performance Matrix">
        <CompactTable rows={[...investment.account_performance, ...investment.strategy_attribution]} limit={8} />
      </Panel>
      <Panel title="Holdings / Positions">
        <CompactTable rows={investment.broker_positions.length ? investment.broker_positions : investment.portfolio} limit={8} />
      </Panel>
    </section>
  );
}

function Segmented<T extends string>({ values, value, onChange }: { values: readonly T[]; value: T; onChange: (value: T) => void }) {
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

function MaestroTab({ openApp, snapshot }: { openApp: (strategyId: string) => void; snapshot: DashboardSnapshot }) {
  const trust = trustSummary(snapshot);
  return (
    <section className="tab-grid maestro-grid">
      <Panel title="Ops Left" aside={<StatusPill tone="warning">Readonly</StatusPill>}>
        <MetricRows
          metrics={[
            { label: "Mode", value: snapshot.header.mode },
            { label: "Freshness", value: trust.freshness, tone: trust.freshnessTone },
            { label: "Reconciliation", value: trust.reconciliation, tone: trust.reconciliationTone },
            { label: "Signal Routes", value: "inspect" },
            { label: "Broker Sync", value: "active", tone: "success" },
          ]}
        />
      </Panel>
      <Panel className="pipeline-panel" title="Operational Ontology Map" aside={<span>readonly: broker sync active</span>}>
        <MaestroOntology snapshot={snapshot} openApp={openApp} />
      </Panel>
      <Panel title="Gate Right" aside={<StatusPill tone="warning">Locked</StatusPill>}>
        <div className="ai-copy">
          <h3>Readonly Meaning</h3>
          <p>Strategy routes are visible for diagnosis. Broker/account sync is the only active operational path in readonly mode.</p>
        </div>
        <MetricRows
          metrics={[
            { label: "Telegram", value: "gated", tone: "warning" },
            { label: "Execution", value: snapshot.header.order_posture, tone: "warning" },
            { label: "Published State", value: snapshot.operator_home.status, tone: toneFromValue(snapshot.operator_home.status) },
          ]}
        />
      </Panel>
      <Panel className="span-2" title="Workflow State Matrix / Audit">
        <CompactTable
          rows={[
            ...snapshot.workflow_pipelines.system.nodes.map((node) => ({
              object: node.label,
              state: node.status,
              updated: node.updated_at_display || node.updated_at,
              run_id: node.run_id,
              attention: node.next_check,
            })),
            ...snapshot.operator_cockpit.health_checks.slice(0, 4),
          ]}
          limit={10}
        />
      </Panel>
    </section>
  );
}

// ─── Ontology Map — Enhanced Visual Components ───

function relativeTime(dateStr: unknown): string {
  if (!dateStr) return "—";
  const ms = Date.now() - new Date(String(dateStr)).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const s = Math.floor(ms / 1000);
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function MiniSparkline({ data, x, y, w, h, tone }: {
  data: number[];
  x: number;
  y: number;
  w: number;
  h: number;
  tone: string;
}) {
  if (data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const pts = data.map((v, i) => ({
    px: x + (i / (data.length - 1)) * w,
    py: y + h - ((v - min) / range) * (h - 2),
  }));
  const linePoints = pts.map((p) => `${p.px},${p.py}`).join(" ");
  const areaPoints = `${linePoints} ${x + w},${y + h} ${x},${y + h}`;
  const color =
    tone === "success" ? "var(--green)" : tone === "warning" ? "var(--amber)" : tone === "danger" ? "var(--red)" : "var(--cyan)";
  const gradId = `spark-${Math.round(x)}-${Math.round(y)}`;
  return (
    <g className="onto-sparkline-group">
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.3" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <polygon points={areaPoints} fill={`url(#${gradId})`} />
      <polyline points={linePoints} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={pts[pts.length - 1].px} cy={pts[pts.length - 1].py} r="2.5" fill={color}>
        <animate attributeName="r" values="2.5;3.5;2.5" dur="2s" repeatCount="indefinite" />
        <animate attributeName="opacity" values="1;0.6;1" dur="2s" repeatCount="indefinite" />
      </circle>
    </g>
  );
}

function StatusIcon({ status, x, y }: { status: string; x: number; y: number }) {
  if (status === "success") {
    return (
      <g transform={`translate(${x} ${y})`} className="status-icon">
        <circle r="9" fill="rgba(61,214,140,0.12)" stroke="rgba(61,214,140,0.2)" strokeWidth="0.5" />
        <path d="M-3.5 0.5 L-1 3 L4 -2.5" fill="none" stroke="var(--green)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </g>
    );
  }
  if (status === "warning") {
    return (
      <g transform={`translate(${x} ${y})`} className="status-icon">
        <circle r="9" fill="rgba(240,180,41,0.12)" stroke="rgba(240,180,41,0.2)" strokeWidth="0.5" />
        <path d="M0 -3 V1" fill="none" stroke="var(--amber)" strokeWidth="2" strokeLinecap="round" />
        <circle cx="0" cy="3.5" r="1" fill="var(--amber)" />
      </g>
    );
  }
  if (status === "danger") {
    return (
      <g transform={`translate(${x} ${y})`} className="status-icon">
        <circle r="9" fill="rgba(240,71,90,0.12)" stroke="rgba(240,71,90,0.2)" strokeWidth="0.5" />
        <path d="M-2.5 -2.5 L2.5 2.5 M2.5 -2.5 L-2.5 2.5" fill="none" stroke="var(--red)" strokeWidth="1.5" strokeLinecap="round" />
      </g>
    );
  }
  return (
    <g transform={`translate(${x} ${y})`} className="status-icon">
      <circle r="9" fill="rgba(255,255,255,0.04)" stroke="rgba(255,255,255,0.06)" strokeWidth="0.5" />
      <circle r="2" fill="var(--faint)" />
    </g>
  );
}

function ParticleTrail({
  path,
  color,
  count = 3,
  duration = 4,
  paused = false,
}: {
  path: string;
  color: string;
  count?: number;
  duration?: number;
  paused?: boolean;
}) {
  if (paused) return null;
  return (
    <g className="particle-trail">
      {Array.from({ length: count }, (_, i) => (
        <circle key={i} r={Math.max(1.5, 3 - i * 0.5)} fill={color} opacity={Math.max(0.2, 0.7 - i * 0.15)}>
          <animateMotion dur={`${duration}s`} repeatCount="indefinite" begin={`${(i * duration) / count}s`} path={path} />
        </circle>
      ))}
      <circle r={7} fill={color} opacity={0.1} filter="url(#particleGlow)">
        <animateMotion dur={`${duration}s`} repeatCount="indefinite" path={path} />
      </circle>
    </g>
  );
}

function AttentionRipple({ cx, cy, color = "var(--amber)" }: { cx: number; cy: number; color?: string }) {
  return (
    <g className="attention-ripple-group">
      <circle cx={cx} cy={cy} fill="none" stroke={color} strokeWidth="1.5" opacity="0">
        <animate attributeName="r" from="12" to="55" dur="2.5s" repeatCount="indefinite" />
        <animate attributeName="opacity" from="0.45" to="0" dur="2.5s" repeatCount="indefinite" />
      </circle>
      <circle cx={cx} cy={cy} fill="none" stroke={color} strokeWidth="1" opacity="0">
        <animate attributeName="r" from="12" to="55" dur="2.5s" repeatCount="indefinite" begin="0.8s" />
        <animate attributeName="opacity" from="0.3" to="0" dur="2.5s" repeatCount="indefinite" begin="0.8s" />
      </circle>
    </g>
  );
}

function OntologyNodeCard({
  id,
  title,
  meta,
  tone = "neutral",
  x,
  y,
  width = 125,
  height = 65,
  sparklineData,
  returnLabel,
  tags = [],
  timeLabel,
  onClick,
  faded = false,
  attentionCount = 0,
  hovered = false,
  activePath = false,
  onHover,
  onLeave,
}: {
  id: string;
  title: string;
  meta: string;
  tone?: string;
  x: number;
  y: number;
  width?: number;
  height?: number;
  sparklineData?: number[];
  returnLabel?: string;
  tags?: string[];
  timeLabel?: string;
  onClick?: () => void;
  faded?: boolean;
  attentionCount?: number;
  hovered?: boolean;
  activePath?: boolean;
  onHover?: (id: string, e: React.MouseEvent) => void;
  onLeave?: () => void;
}) {
  const hasSparkline = sparklineData && sparklineData.length >= 2;
  const effectiveHeight = hasSparkline ? Math.max(height, 120) : tags.length ? Math.max(height, 82) : height;
  const toneColor =
    tone === "success"
      ? "var(--green)"
      : tone === "warning"
        ? "var(--amber)"
        : tone === "danger"
          ? "var(--red)"
          : tone === "primary"
            ? "var(--cyan)"
            : "rgba(255,255,255,0.12)";

  return (
    <g
      className={[
        "onto-card-group",
        faded ? "onto-faded" : "",
        hovered ? "onto-hovered" : "",
        activePath ? "onto-active-path" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      transform={`translate(${x} ${y})`}
      onClick={onClick}
      onMouseEnter={(e) => onHover?.(id, e)}
      onMouseLeave={() => onLeave?.()}
      role={onClick ? "button" : undefined}
    >
      {/* Outer glow */}
      <rect
        x={-5}
        y={-5}
        width={width + 10}
        height={effectiveHeight + 10}
        rx={12}
        ry={12}
        fill="none"
        stroke={toneColor}
        strokeWidth={hovered || activePath ? 1.8 : 0.8}
        opacity={hovered || activePath ? 0.5 : 0.12}
        filter="url(#nodeGlow)"
      />
      {/* Card bg */}
      <rect
        className="onto-card-bg"
        width={width}
        height={effectiveHeight}
        rx={6}
        ry={6}
        fill="url(#cardFill)"
        stroke={toneColor}
        strokeWidth={hovered || activePath ? 1.2 : 0.6}
      />
      {/* Top accent */}
      <rect x={8} y={1.5} width={width - 16} height={2} rx={1} fill={toneColor} opacity={0.55} />
      {/* Glass highlight */}
      <rect x={1} y={1} width={width - 2} height={22} rx={6} ry={6} fill="url(#cardHighlight)" />
      {/* Status icon */}
      <StatusIcon status={tone} x={width - 16} y={16} />
      {/* Title */}
      <text className="onto-card-title" x={10} y={20}>
        {title}
      </text>
      {/* Meta */}
      <text className="onto-card-meta" x={10} y={35}>
        {meta}
      </text>
      {/* Sparkline */}
      {hasSparkline && <MiniSparkline data={sparklineData} x={10} y={42} w={width - 20} h={28} tone={tone} />}
      {/* Return */}
      {returnLabel && (
        <text
          className={`onto-card-return ${Number.parseFloat(returnLabel) >= 0 ? "tone-success" : "tone-danger"}`}
          x={width - 10}
          y={hasSparkline ? 82 : 35}
          textAnchor="end"
        >
          {returnLabel}
        </text>
      )}
      {/* Time */}
      {timeLabel && (
        <text className="onto-card-time" x={10} y={hasSparkline ? 82 : 52}>
          {timeLabel}
        </text>
      )}
      {/* Tags */}
      {tags.length > 0 && (
        <g transform={`translate(10 ${effectiveHeight - 24})`}>
          {tags.map((tag, i) => (
            <g key={tag} transform={`translate(${i * 54} 0)`}>
              <rect
                className="onto-tag"
                width="50"
                height="18"
                rx={4}
                ry={4}
                fill={
                  tone === "success"
                    ? "rgba(61,214,140,0.08)"
                    : tone === "warning"
                      ? "rgba(240,180,41,0.06)"
                      : "rgba(34,211,238,0.06)"
                }
                stroke={
                  tone === "success"
                    ? "rgba(61,214,140,0.2)"
                    : tone === "warning"
                      ? "rgba(240,180,41,0.15)"
                      : "rgba(34,211,238,0.15)"
                }
                strokeWidth={0.8}
              />
              <text className="onto-micro" x={6} y={12}>
                {tag}
              </text>
            </g>
          ))}
        </g>
      )}
      {/* Attention badge */}
      {attentionCount > 0 && (
        <g transform={`translate(${width - 3} -3)`}>
          <circle r={8} fill="var(--red)" stroke="#020408" strokeWidth={1.5} />
          <text fill="#fff" textAnchor="middle" y={3.5} style={{ font: "800 9px var(--mono)" }}>
            {attentionCount}
          </text>
        </g>
      )}
    </g>
  );
}

function EdgePill({ text, tone, x, y }: { text: string; tone: Tone; x: number; y: number }) {
  return (
    <g transform={`translate(${x} ${y})`}>
      <rect className={`edge-pill ${toneClass(tone)}`} width="110" height="19" rx={4} ry={4} />
      <text className="onto-micro" x="8" y="13">
        {text}
      </text>
    </g>
  );
}

function MaestroOntology({
  openApp,
  snapshot,
}: {
  openApp: (strategyId: string) => void;
  snapshot: DashboardSnapshot;
}) {
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const [activeStrategyId, setActiveStrategyId] = useState<string | null>(null);
  const [tooltipInfo, setTooltipInfo] = useState<{ x: number; y: number; data: Record<string, string> } | null>(null);
  const [initHighlight, setInitHighlight] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const timer = setTimeout(() => setInitHighlight(false), 2500);
    return () => clearTimeout(timer);
  }, []);

  const apps = snapshot.workflow_pipelines.apps;
  const strategies = snapshot.virtuoso_apps.strategies;
  const freshness = snapshot.virtuoso_apps.signal_freshness;
  const attribution = snapshot.investment_console.strategy_attribution;
  const mode =
    snapshot.read_only || snapshot.header.mode.includes("readonly")
      ? "readonly"
      : snapshot.header.mode.includes("signal")
        ? "signal"
        : "approval";

  // ── Derive per-strategy data ──
  const strategyData = strategies.map((s) => {
    const signal = freshness.strategies.find((f) => f.strategy_id === s.strategy_id);
    const attr = attribution.find((a) => String(a.strategy_id) === s.strategy_id);
    const perf = s.performance
      .map((p) => Number(p.current_value))
      .filter(Number.isFinite)
      .reverse();
    const ret = s.summary.cumulative_return;
    const retLabel =
      ret != null ? `${Number(ret) >= 0 ? "+" : ""}${(Number(ret) * 100).toFixed(1)}%` : undefined;
    const signalStatus = signal?.status || "missing";
    return {
      strategyId: s.strategy_id,
      displayName:
        apps.find((a) => a.strategy_id === s.strategy_id)?.display_name || humanize(s.strategy_id),
      accountId: apps.find((a) => a.strategy_id === s.strategy_id)?.account_id,
      signalStatus,
      signalAt: signal?.latest_signal_at,
      weight: Number(attr?.weight) || 0,
      perf,
      retLabel,
      tone:
        signalStatus === "fresh"
          ? "success"
          : signalStatus === "stale"
            ? "warning"
            : signalStatus === "missing"
              ? "warning"
              : "danger",
      enabled: s.operation.some((o) => String(o.value).toLowerCase() === "true"),
    };
  });

  const primary = strategyData[0];
  const second = strategyData[1];
  const third = strategyData[2];

  // ── Layout constants ──
  // ViewBox 720×455. Columns sized to fill most of the available width.
  // KIS REST is below DATA column, connects left→Accounts→right→Gate/State
  const COL = {
    dataX: 8,
    dataW: 125,
    virtX: 153,
    virtW: 155,
    acctX: 415,
    acctW: 135,
    gateX: 572,
    gateW: 140,
  };

  // ── Edge paths ──
  const edgePaths: Record<string, string> = {
    dataMarket: `M${COL.dataX + COL.dataW} 60 H${COL.virtX}`,
    dataMacro: `M${COL.dataX + COL.dataW} 137 H${COL.virtX}`,
    dataNarrative: `M${COL.dataX + COL.dataW} 207 H${COL.virtX}`,
    primaryToAccount: `M${COL.virtX + COL.virtW} 82 H${COL.acctX}`,
    secondToAccount: `M${COL.virtX + COL.virtW} 196 H${COL.acctX - 12} V191 H${COL.acctX}`,
    thirdToAccount: `M${COL.virtX + COL.virtW} 285 H${COL.acctX - 12} V260 H${COL.acctX}`,
    // KIS REST → right → enters Accounts from left
    kisRestToAccounts: `M${COL.dataX + COL.dataW} 387 H${COL.acctX - 22} V75 H${COL.acctX}`,
    // Accounts right side → Operating Gate
    primaryAccountToGate: `M${COL.acctX + COL.acctW} 63 H${COL.gateX}`,
    // Accounts right side → State/Audit
    accountToState: `M${COL.acctX + COL.acctW} 100 H${COL.gateX - 12} V387 H${COL.gateX}`,
  };

  function isOnActivePath(edgeKey: string): boolean {
    if (!activeStrategyId) return false;
    if (activeStrategyId === primary?.strategyId) {
      return ["dataMarket", "dataMacro", "primaryToAccount", "primaryAccountToGate", "accountToState"].includes(edgeKey);
    }
    if (activeStrategyId === second?.strategyId) {
      return ["dataMarket", "dataMacro", "secondToAccount"].includes(edgeKey);
    }
    if (activeStrategyId === third?.strategyId) {
      return ["dataMarket", "dataMacro", "thirdToAccount"].includes(edgeKey);
    }
    return false;
  }

  function edgeTypeOf(key: string): string {
    if (key.startsWith("data")) return "data";
    if (key.includes("ToAccount") || key.includes("kisRest")) return "signal";
    if (key.includes("ToGate")) return "gate";
    if (key.includes("ToState") || key.includes("accountToState")) return "state";
    return "state";
  }

  function edgeColorOf(type: string): string {
    if (type === "data") return "var(--cyan)";
    if (type === "signal") return "var(--blue)";
    if (type === "gate") return "var(--amber)";
    if (type === "state") return "var(--green)";
    return "var(--green)";
  }

  function getTooltipData(nodeId: string): Record<string, string> {
    const sd = strategyData.find((s) => s.strategyId === nodeId);
    if (sd) {
      return {
        Signal: sd.signalStatus,
        Return: sd.retLabel || "n/a",
        Weight: `${(sd.weight * 100).toFixed(1)}%`,
        Updated: relativeTime(sd.signalAt),
      };
    }
    if (nodeId === "gate") {
      return { Posture: snapshot.header.order_posture, Mode: mode, Telegram: "gated" };
    }
    if (nodeId.startsWith("account-")) {
      const accId = nodeId.replace("account-", "");
      const acc = snapshot.investment_console.broker_account_overview.accounts.find(
        (a) => String(a.account_id) === accId,
      );
      return { Status: String(acc?.status || "available"), Updated: relativeTime(acc?.created_at) };
    }
    if (nodeId === "market" || nodeId === "macro") return { Feed: "active", Status: "fresh" };
    return { Status: "operational" };
  }

  function handleNodeHover(nodeId: string, e: React.MouseEvent) {
    setHoveredNodeId(nodeId);
    const container = containerRef.current;
    if (!container) return;
    const rect = container.getBoundingClientRect();
    setTooltipInfo({
      x: Math.min(e.clientX - rect.left + 16, rect.width - 240),
      y: Math.min(Math.max(e.clientY - rect.top - 10, 10), rect.height - 120),
      data: getTooltipData(nodeId),
    });
  }

  function handleNodeLeave() {
    setHoveredNodeId(null);
    setTooltipInfo(null);
  }

  function handleStrategyClick(strategyId: string) {
    setActiveStrategyId((prev) => (prev === strategyId ? null : strategyId));
    openApp(strategyId);
  }

  const hasAttentionNodes = strategyData.some((s) => s.enabled && s.signalStatus !== "fresh");

  return (
    <div className="ontology-frame" data-mode={mode} ref={containerRef}>
      <svg
        viewBox="0 0 720 455"
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label="Maestro operational ontology map"
      >
        <defs>
          <linearGradient id="cardFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#151e2c" stopOpacity="0.95" />
            <stop offset="100%" stopColor="#0a1018" stopOpacity="0.92" />
          </linearGradient>
          <linearGradient id="cardHighlight" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="0.05" />
            <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
          </linearGradient>
          <filter id="nodeGlow" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="3" />
          </filter>
          <filter id="particleGlow" x="-100%" y="-100%" width="300%" height="300%">
            <feGaussianBlur stdDeviation="3" />
          </filter>
          <pattern
            id="hatchPattern"
            width="6"
            height="6"
            patternUnits="userSpaceOnUse"
            patternTransform="rotate(45)"
          >
            <line x1="0" y1="0" x2="0" y2="6" stroke="rgba(240,180,41,0.06)" strokeWidth="2" />
          </pattern>
        </defs>

        {/* ── Lane Background Bands ── */}
        <rect className="lane-band lane-data" x={3} y={20} width={135} height={320} rx={5} />
        <rect className="lane-band lane-virtuoso" x={145} y={20} width={170} height={320} rx={5} />
        <rect className="lane-band lane-accounts" x={407} y={20} width={145} height={320} rx={5} />
        <rect className="lane-band lane-gate" x={564} y={20} width={150} height={320} rx={5} />

        {/* Lane Labels */}
        <text className="lane-label" x={70} y={14}>
          DATA
        </text>
        <text className="lane-label" x={230} y={14}>
          VIRTUOSO
        </text>
        <text className="lane-label" x={480} y={14}>
          ACCOUNTS
        </text>
        <text className="lane-label" x={639} y={14}>
          OPERATING GATE
        </text>

        <line className="lane-separator" x1={3} y1={20} x2={714} y2={20} />
        <line className="lane-separator" x1={3} y1={340} x2={714} y2={340} />

        {/* Sub-lane labels below separator */}
        <text className="lane-label" x={70} y={355}>
          BROKER SYNC
        </text>
        <text className="lane-label" x={639} y={355}>
          PUBLISHED STATE
        </text>

        {/* ── Edges ── */}
        {Object.entries(edgePaths).map(([key, path]) => {
          const active = isOnActivePath(key);
          const isMuted = key === "dataNarrative";
          const type = edgeTypeOf(key);
          const color = edgeColorOf(type);
          return (
            <g key={key}>
              <path
                d={path}
                fill="none"
                stroke={color}
                strokeWidth={active ? 2 : isMuted ? 0.8 : 1.2}
                opacity={isMuted ? 0.06 : active ? 0.65 : 0.2}
                strokeLinecap="round"
                strokeLinejoin="round"
                className={`onto-edge${active ? " onto-edge-active" : ""}${activeStrategyId && !active ? " onto-edge-dim" : ""}`}
              />
              {!isMuted && (type === "state" || key === "kisRestToAccounts" || active) && (
                <ParticleTrail
                  path={path}
                  color={color}
                  count={active ? 4 : 3}
                  duration={type === "state" ? 5 : key === "kisRestToAccounts" ? 4.5 : 3.5}
                />
              )}
            </g>
          );
        })}

        {/* ── Signal OFF (readonly) ── */}
        {mode === "readonly" && (
          <g className="signal-off-group">
            <rect x={320} y={22} width={82} height={315} fill="url(#hatchPattern)" rx={3} />
            <rect x={320} y={22} width={82} height={315} rx={3} fill="rgba(0,0,0,0.06)" />
            <rect
              x={325}
              y={26}
              width={72}
              height={19}
              rx={10}
              fill="rgba(240,180,41,0.1)"
              stroke="rgba(240,180,41,0.25)"
              strokeWidth={0.8}
            />
            <text className="signal-off-text" x={361} y={39} textAnchor="middle">
              SIGNAL OFF
            </text>
          </g>
        )}

        {/* ── Edge Pills ── */}
        <EdgePill x={320} y={72} text="BUY-ONLY CONTRIB" tone="primary" />
        <EdgePill x={320} y={187} text="TARGET ALLOC" tone="warning" />
        <EdgePill x={320} y={278} text="TARGET ALLOC" tone="neutral" />

        {/* ── Attention Ripples ── */}
        {second && second.signalStatus === "missing" && second.enabled && (
          <AttentionRipple cx={COL.virtX + COL.virtW / 2} cy={196} color="var(--amber)" />
        )}

        {/* ── Init highlight flash ── */}
        {initHighlight && hasAttentionNodes && (
          <rect className="init-highlight-flash" x={145} y={20} width={170} height={320} rx={5} />
        )}

        {/* ── DATA column ── */}
        <OntologyNodeCard
          id="market"
          title="Market"
          meta="Yahoo Finance"
          tone="success"
          x={COL.dataX}
          y={30}
          width={COL.dataW}
          height={60}
          timeLabel={relativeTime(snapshot.operator_cockpit.health_checks[0]?.updated_at)}
          hovered={hoveredNodeId === "market"}
          activePath={!!activeStrategyId}
          onHover={handleNodeHover}
          onLeave={handleNodeLeave}
        />
        <OntologyNodeCard
          id="macro"
          title="Macro"
          meta="FRED Economic"
          tone="success"
          x={COL.dataX}
          y={105}
          width={COL.dataW}
          height={60}
          timeLabel={relativeTime(snapshot.operator_cockpit.health_checks[2]?.updated_at)}
          hovered={hoveredNodeId === "macro"}
          activePath={!!activeStrategyId}
          onHover={handleNodeHover}
          onLeave={handleNodeLeave}
        />
        <OntologyNodeCard
          id="narrative"
          title="Narrative"
          meta="News / GDELT"
          tone="neutral"
          x={COL.dataX}
          y={180}
          width={COL.dataW}
          height={52}
          faded
          hovered={hoveredNodeId === "narrative"}
          onHover={handleNodeHover}
          onLeave={handleNodeLeave}
        />

        {/* ── VIRTUOSO column — Strategy nodes ── */}
        {primary && (
          <OntologyNodeCard
            id={primary.strategyId}
            title={primary.displayName}
            meta={primary.enabled ? "selected app" : "disabled"}
            tone={primary.tone}
            x={COL.virtX}
            y={28}
            width={COL.virtW}
            height={120}
            sparklineData={primary.perf}
            returnLabel={primary.retLabel}
            tags={primary.enabled ? ["ON", primary.signalStatus === "fresh" ? "FRESH" : "STALE"] : []}
            timeLabel={relativeTime(primary.signalAt)}
            onClick={() => handleStrategyClick(primary.strategyId)}
            hovered={hoveredNodeId === primary.strategyId}
            activePath={activeStrategyId === primary.strategyId}
            onHover={handleNodeHover}
            onLeave={handleNodeLeave}
          />
        )}
        {second && (
          <OntologyNodeCard
            id={second.strategyId}
            title={second.displayName}
            meta={`signal ${second.signalStatus}`}
            tone={second.tone}
            x={COL.virtX}
            y={157}
            width={COL.virtW}
            height={100}
            sparklineData={second.perf}
            returnLabel={second.retLabel}
            attentionCount={second.enabled && second.signalStatus !== "fresh" ? 1 : 0}
            timeLabel={relativeTime(second.signalAt)}
            onClick={() => handleStrategyClick(second.strategyId)}
            hovered={hoveredNodeId === second.strategyId}
            activePath={activeStrategyId === second.strategyId}
            onHover={handleNodeHover}
            onLeave={handleNodeLeave}
          />
        )}
        {third && (
          <OntologyNodeCard
            id={third.strategyId}
            title={third.displayName}
            meta={third.enabled ? "enabled" : "disabled"}
            tone={third.enabled ? third.tone : "neutral"}
            x={COL.virtX}
            y={265}
            width={COL.virtW}
            height={68}
            sparklineData={third.perf.length >= 2 ? third.perf : undefined}
            returnLabel={third.retLabel}
            faded={!third.enabled}
            timeLabel={relativeTime(third.signalAt)}
            onClick={() => handleStrategyClick(third.strategyId)}
            hovered={hoveredNodeId === third.strategyId}
            activePath={activeStrategyId === third.strategyId}
            onHover={handleNodeHover}
            onLeave={handleNodeLeave}
          />
        )}

        {/* ── ACCOUNTS column ── */}
        <OntologyNodeCard
          id={`account-${primary?.accountId || "KIS_mock"}`}
          title={primary?.accountId || "KIS_mock"}
          meta="primary target"
          tone="success"
          x={COL.acctX}
          y={30}
          width={COL.acctW}
          height={75}
          tags={["SYNCED"]}
          timeLabel={relativeTime(snapshot.investment_console.broker_snapshot.created_at)}
          hovered={hoveredNodeId === `account-${primary?.accountId || "KIS_mock"}`}
          activePath={activeStrategyId === primary?.strategyId}
          onHover={handleNodeHover}
          onLeave={handleNodeLeave}
        />
        <OntologyNodeCard
          id="account-KIS_ps"
          title="KIS_ps"
          meta="available account"
          tone="neutral"
          x={COL.acctX}
          y={118}
          width={COL.acctW}
          height={52}
          hovered={hoveredNodeId === "account-KIS_ps"}
          onHover={handleNodeHover}
          onLeave={handleNodeLeave}
        />
        <OntologyNodeCard
          id={`account-${second?.accountId || "DEV_sandbox"}`}
          title={second?.accountId || "DEV_sandbox"}
          meta="dev target"
          tone="warning"
          x={COL.acctX}
          y={180}
          width={COL.acctW}
          height={55}
          hovered={hoveredNodeId === `account-${second?.accountId || "DEV_sandbox"}`}
          activePath={activeStrategyId === second?.strategyId}
          onHover={handleNodeHover}
          onLeave={handleNodeLeave}
        />
        <OntologyNodeCard
          id="account-KIS_brokerage"
          title="KIS_brokerage"
          meta="readonly account"
          tone="neutral"
          x={COL.acctX}
          y={245}
          width={COL.acctW}
          height={50}
          hovered={hoveredNodeId === "account-KIS_brokerage"}
          onHover={handleNodeHover}
          onLeave={handleNodeLeave}
        />

        {/* ── GATE column ── */}
        <OntologyNodeCard
          id="gate"
          title="Operating Gate"
          meta="Telegram + execution"
          tone="warning"
          x={COL.gateX}
          y={28}
          width={COL.gateW}
          height={95}
          tags={["GATED", "AUDIT"]}
          hovered={hoveredNodeId === "gate"}
          activePath={activeStrategyId === primary?.strategyId}
          onHover={handleNodeHover}
          onLeave={handleNodeLeave}
        />

        {/* ── Bottom row: KIS REST (below data) and State/Audit (below gate) ── */}
        <OntologyNodeCard
          id="kis-rest"
          title="KIS REST"
          meta="readonly sync"
          tone="success"
          x={COL.dataX}
          y={365}
          width={COL.dataW}
          height={55}
          hovered={hoveredNodeId === "kis-rest"}
          onHover={handleNodeHover}
          onLeave={handleNodeLeave}
        />
        <OntologyNodeCard
          id="state-audit"
          title="State / Audit"
          meta="read model"
          tone="neutral"
          x={COL.gateX}
          y={365}
          width={COL.gateW}
          height={55}
          hovered={hoveredNodeId === "state-audit"}
          onHover={handleNodeHover}
          onLeave={handleNodeLeave}
        />

        {/* ── Connection dots on Accounts ── */}
        <circle cx={COL.acctX} cy={75} r={3} fill="var(--green)" opacity={0.5}>
          <animate attributeName="opacity" values="0.3;0.7;0.3" dur="3s" repeatCount="indefinite" />
        </circle>
        <circle cx={COL.acctX + COL.acctW} cy={63} r={3} fill="var(--amber)" opacity={0.5}>
          <animate attributeName="opacity" values="0.3;0.7;0.3" dur="3s" repeatCount="indefinite" />
        </circle>
        <circle cx={COL.acctX + COL.acctW} cy={100} r={3} fill="var(--green)" opacity={0.5}>
          <animate attributeName="opacity" values="0.3;0.7;0.3" dur="3s" repeatCount="indefinite" begin="0.5s" />
        </circle>

        {/* ── Legend ── */}
        <g transform="translate(8 442)">
          <text className="onto-legend-title" x={0} y={0}>
            LEGEND
          </text>
          {[
            { color: "var(--green)", label: "Healthy" },
            { color: "var(--amber)", label: "Attention" },
            { color: "var(--red)", label: "Failed" },
            { color: "var(--cyan)", label: "Data" },
            { color: "var(--blue)", label: "Signal" },
          ].map((item, i) => (
            <g key={item.label} transform={`translate(${60 + i * 80} -4)`}>
              <circle r={3.5} fill={item.color} opacity={0.8} />
              <text className="onto-legend-text" x={8} y={4}>
                {item.label}
              </text>
            </g>
          ))}
        </g>
      </svg>

      {/* ── HTML Tooltip Overlay ── */}
      {tooltipInfo && (
        <div className="onto-tooltip" style={{ left: tooltipInfo.x, top: tooltipInfo.y }}>
          {Object.entries(tooltipInfo.data).map(([key, value]) => (
            <div className="onto-tooltip-row" key={key}>
              <span className="onto-tooltip-label">{key}</span>
              <span className={`onto-tooltip-value ${toneClass(toneFromValue(value))}`}>{value}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


function VirtuosoTab({
  generatingStrategyId,
  onGenerateSignal,
  selectedStrategyId,
  setSelectedStrategyId,
  signalAction,
  snapshot,
}: {
  generatingStrategyId: string;
  onGenerateSignal: (strategyId: string) => void;
  selectedStrategyId: string;
  setSelectedStrategyId: (strategyId: string) => void;
  signalAction: SignalActionState | null;
  snapshot: DashboardSnapshot;
}) {
  const strategies = snapshot.virtuoso_apps.strategies;
  const selected = latestStrategy(snapshot, selectedStrategyId) || strategies[0];
  useEffect(() => {
    if (!selectedStrategyId && strategies[0]) {
      setSelectedStrategyId(strategies[0].strategy_id);
    }
  }, [selectedStrategyId, setSelectedStrategyId, strategies]);

  if (!selected) {
    return <ShellMessage title="Virtuoso" copy="No configured Virtuoso app is available in the current snapshot." />;
  }

  const signal = latestSignal(snapshot, selected.strategy_id);
  const perfRows = selected.performance_snapshot?.series?.value?.length ? selected.performance_snapshot.series.value : selected.performance;
  const yKey = perfRows.some((row) => Number.isFinite(Number(row.current_value))) ? "current_value" : "book_value";
  const action = signalAction?.strategyId === selected.strategy_id ? signalAction : null;

  return (
    <section className="tab-grid virtuoso-grid">
      <Panel title="App List" aside={<StatusPill tone="success">{strategies.length} apps</StatusPill>}>
        <div className="app-list">
          {strategies.map((strategy) => (
            <button
              className={strategy.strategy_id === selected.strategy_id ? "app-list-item active" : "app-list-item"}
              key={strategy.strategy_id}
              type="button"
              onClick={() => setSelectedStrategyId(strategy.strategy_id)}
            >
              <b>{appName(strategy.strategy_id)}</b>
              <span>{formatValue(strategy.summary.current_value ?? strategy.summary.book_value ?? selected.performance_snapshot?.quality?.status)}</span>
            </button>
          ))}
        </div>
      </Panel>
      <Panel className="app-terminal-panel" title={`${appName(selected.strategy_id)} App Terminal`} aside={<span>selected app detail</span>}>
        <div className="app-terminal">
          <div className="app-kpis">
            {strategyMetrics(selected).map((metric) => (
              <div className="app-kpi" key={metric.label}>
                <b>{metric.label}</b>
                <span className={toneClass(metric.tone)}>{formatValue(metric.value)}</span>
                <small>{metric.label === "Signal" ? formatValue(signal?.latest_signal_at || "No signal package") : "snapshot read model"}</small>
              </div>
            ))}
          </div>
          <div className="app-center-grid">
            <TerminalChart title="App Performance" rows={perfRows} yKey={yKey} markers={selected.performance_snapshot?.series?.cash_flow_markers || []} />
            <div className="app-detail-stack">
              <Panel title="Latest Proposal">
                <MetricRows
                  metrics={[
                    { label: "Intent", value: rowValue(selected.runs[0], ["signal_action", "action", "status"]) },
                    { label: "Account", value: snapshot.workflow_pipelines.apps.find((app) => app.strategy_id === selected.strategy_id)?.account_id || "n/a" },
                    { label: "Run", value: selected.runs[0]?.run_id || signal?.latest_signal_run_id || "n/a" },
                    { label: "Gate", value: snapshot.header.order_posture, tone: "warning" },
                  ]}
                />
              </Panel>
              <Panel title="Config / Inputs">
                <CompactTable rows={[selected.config || {}, ...selected.concept.slice(0, 2)]} limit={4} />
              </Panel>
            </div>
          </div>
          {action && <p className={`action-status ${toneClass(action.tone)}`}>{action.message}</p>}
          <div className="app-actions">
            <TerminalButton disabled={!!generatingStrategyId} onClick={() => onGenerateSignal(selected.strategy_id)} variant="primary">
              {generatingStrategyId === selected.strategy_id ? "Generating" : "Generate Signal"}
            </TerminalButton>
            <span>Proposal signal only. No orders or approvals are submitted.</span>
          </div>
        </div>
      </Panel>
      <Panel title="Selected App Summary">
        <div className="summary-pills">
          <SummaryPill label="Signal Type" value={rowValue(selected.runs[0], ["signal_action", "action"]) || "proposal"} />
          <SummaryPill label="Freshness" value={formatValue(signal?.status || "missing")} />
          <SummaryPill label="Evidence" value={formatValue(selected.performance_snapshot?.quality?.status || "missing")} />
          <SummaryPill label="AI Note" value="Inspect app evidence here; use Research for comparison before changing parameters." />
        </div>
      </Panel>
      <Panel className="span-2" title="Signals / Performance / Evidence / Orders">
        <CompactTable
          rows={[
            ...selected.runs.slice(0, 4).map((row) => ({ area: "Signals", ...row })),
            ...selected.snapshots.slice(0, 4).map((row) => ({ area: "Evidence", ...row })),
            ...snapshot.audit_trail.orders.filter((row) => String(row.strategy_id || "") === selected.strategy_id).map((row) => ({ area: "Orders", ...row })),
          ]}
          limit={10}
        />
      </Panel>
    </section>
  );
}

function SummaryPill({ label, value }: { label: string; value: string }) {
  return (
    <div className="summary-pill">
      <b>{label}</b>
      <span>{value}</span>
    </div>
  );
}

function ResearchTab({ snapshot }: { snapshot: DashboardSnapshot }) {
  const [selectedStrategyId, setSelectedStrategyId] = useState(snapshot.virtuoso_apps.strategies[0]?.strategy_id || "");
  const selected = latestStrategy(snapshot, selectedStrategyId) || snapshot.virtuoso_apps.strategies[0];
  const perfRows = selected?.performance_snapshot?.series?.value?.length ? selected.performance_snapshot.series.value : selected?.performance || [];
  const yKey = perfRows.some((row) => Number.isFinite(Number(row.current_value))) ? "current_value" : "book_value";
  const runs = snapshot.virtuoso_apps.strategies.flatMap((strategy) =>
    strategy.runs.map((run) => ({
      app: appName(strategy.strategy_id),
      strategy_id: strategy.strategy_id,
      status: firstValue(run, ["status", "validation_ok", "mode"]) ?? "persisted",
      created_at: run.created_at,
      run_id: run.run_id,
      evidence: strategy.performance_snapshot?.quality?.status || "missing",
    })),
  );
  return (
    <section className="tab-grid research-grid">
      <Panel title="Research Control" aside={<StatusPill tone="primary">Read model</StatusPill>}>
        <label className="field">
          <span>Virtuoso App</span>
          <select value={selectedStrategyId} onChange={(event) => setSelectedStrategyId(event.target.value)}>
            {snapshot.virtuoso_apps.strategies.map((strategy) => (
              <option key={strategy.strategy_id} value={strategy.strategy_id}>{appName(strategy.strategy_id)}</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Saved Preset</span>
          <select disabled>
            <option>Research presets deferred</option>
          </select>
        </label>
        <div className="disabled-actions">
          <TerminalButton disabled>Run Backtest</TerminalButton>
          <TerminalButton disabled>Save Preset</TerminalButton>
        </div>
        <p className="muted-copy">Queued jobs and preset writes are deferred. This tab reviews persisted strategy evidence only.</p>
      </Panel>
      <Panel className="main-chart-panel" title="Research Performance / Evidence Compare">
        <TerminalChart title="Selected App Evidence" rows={perfRows} yKey={yKey} markers={selected?.performance_snapshot?.series?.cash_flow_markers || []} />
      </Panel>
      <Panel title="AI Interpretation">
        <div className="ai-copy">
          <h3>Read-model Review</h3>
          <p>Use this tab to compare persisted app performance, signal runs, and evidence quality before planning any queued backtest work.</p>
        </div>
        <MetricRows metrics={selected ? strategyMetrics(selected) : []} />
      </Panel>
      <Panel className="span-2" title="Run Comparison Table">
        <CompactTable rows={runs} limit={12} columns={["app", "status", "evidence", "created_at", "run_id"]} />
      </Panel>
      <Panel title="Evidence Summaries">
        <CompactTable rows={evidenceSummaries(snapshot, "").slice(0, 10)} limit={10} />
      </Panel>
    </section>
  );
}

function ConsoleDrawer({
  copyState,
  diagnosticContext,
  open,
  query,
  selectedRunId,
  setCopyState,
  setOpen,
  setQuery,
  setSelectedRunId,
  snapshot,
}: {
  copyState: string;
  diagnosticContext: string;
  open: boolean;
  query: string;
  selectedRunId: string;
  setCopyState: (state: string) => void;
  setOpen: (open: boolean) => void;
  setQuery: (query: string) => void;
  setSelectedRunId: (runId: string) => void;
  snapshot: DashboardSnapshot;
}) {
  async function copyDiagnostic() {
    try {
      await navigator.clipboard.writeText(diagnosticContext);
      setCopyState("Copied");
      window.setTimeout(() => setCopyState("Copy diagnostic context"), 1600);
    } catch {
      setCopyState("Copy failed");
      window.setTimeout(() => setCopyState("Copy diagnostic context"), 1600);
    }
  }
  return (
    <aside className={open ? "console-drawer open" : "console-drawer"} aria-label="Console drawer">
      <header className="console-head">
        <div>
          <span className="eyebrow">Console</span>
          <h2>Evidence without raw logs</h2>
        </div>
        <button type="button" onClick={() => setOpen(false)} aria-label="Close console">✕</button>
      </header>
      <label className="field">
        <span>Search summaries</span>
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="run, stale, broker, approval" />
      </label>
      <label className="field">
        <span>Run context</span>
        <select value={selectedRunId} onChange={(event) => setSelectedRunId(event.target.value)}>
          {snapshot.audit_trail.run_index.map((row) => (
            <option key={String(row.run_id)} value={String(row.run_id)}>{String(row.run_id)}</option>
          ))}
        </select>
      </label>
      <TerminalButton onClick={() => void copyDiagnostic()} variant="primary">{copyState}</TerminalButton>
      <Panel title="Evidence Summaries">
        <CompactTable rows={evidenceSummaries(snapshot, query)} limit={10} />
      </Panel>
    </aside>
  );
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

function lastUpdatedMessage() {
  return "Last updated: " + formatValue(new Date().toISOString());
}

export function App() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [displayCurrency, setDisplayCurrency] = useState<"KRW" | "USD">("KRW");
  const [activeTab, setActiveTab] = useState<TabName>("Portfolio");
  const [period, setPeriod] = useState<Period>("30D");
  const [consoleOpen, setConsoleOpen] = useState(false);
  const [consoleQuery, setConsoleQuery] = useState("");
  const [selectedRunId, setSelectedRunId] = useState("");
  const [selectedStrategyId, setSelectedStrategyId] = useState("");
  const [copyState, setCopyState] = useState("Copy diagnostic context");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshAction, setRefreshAction] = useState<ActionState>({
    busy: false,
    message: "Last updated: not yet refreshed",
    tone: "neutral",
  });
  const [signalAction, setSignalAction] = useState<SignalActionState>({
    busy: false,
    message: "",
    strategyId: "",
    tone: "neutral",
  });

  useEffect(() => {
    void loadSnapshot(displayCurrency, setSnapshot, setLoading, setError);
  }, [displayCurrency]);

  useEffect(() => {
    if (!snapshot || selectedRunId) {
      return;
    }
    const firstRunId = String(snapshot.audit_trail.run_index[0]?.run_id || "");
    if (firstRunId) {
      setSelectedRunId(firstRunId);
    }
  }, [selectedRunId, snapshot]);

  async function handleRefresh() {
    setRefreshAction({ busy: true, message: "Updating...", tone: "primary" });
    try {
      await refreshDashboardState();
      setSnapshot(await fetchSnapshot(displayCurrency));
      setRefreshAction({ busy: false, message: lastUpdatedMessage(), tone: "success" });
    } catch (refreshError) {
      setRefreshAction({ busy: false, message: errorMessage(refreshError, "Dashboard refresh failed"), tone: "danger" });
    }
  }

  async function handleGenerateSignal(strategyId: string) {
    setSelectedStrategyId(strategyId);
    setSignalAction({
      busy: true,
      message: `Generating proposal signal for ${strategyId}...`,
      strategyId,
      tone: "primary",
    });
    try {
      const result = await generateStrategySignal(strategyId);
      setSnapshot(await fetchSnapshot(displayCurrency));
      setSignalAction({
        busy: false,
        message: `Proposal signal ready${result.signal_run_id ? `: ${result.signal_run_id}` : ""}. ${result.orders_preview_count} preview order(s); no orders submitted.`,
        strategyId,
        tone: result.action_required ? "warning" : "success",
      });
    } catch (signalError) {
      setSignalAction({
        busy: false,
        message: errorMessage(signalError, "Signal generation failed"),
        strategyId,
        tone: "danger",
      });
    }
  }

  if (loading && !snapshot) {
    return <ShellMessage title="Maestro" copy="Loading terminal dashboard..." />;
  }

  if (error || !snapshot) {
    return <ShellMessage title="Maestro" copy={error || "Dashboard snapshot is unavailable."} tone="danger" />;
  }

  const selectedStrategy = snapshot.virtuoso_apps.strategies.find((strategy) => strategy.strategy_id === selectedStrategyId);
  const diagnosticContext = buildDiagnosticContext(
    snapshot,
    activeTab,
    displayCurrency,
    period,
    selectedRunId,
    selectedStrategy?.strategy_id || "",
  );

  function openVirtuosoApp(strategyId: string) {
    setSelectedStrategyId(strategyId);
    setActiveTab("Virtuoso");
  }

  return (
    <div className={consoleOpen ? "terminal-shell console-is-open" : "terminal-shell"}>
      <TopChrome
        activeTab={activeTab}
        consoleOpen={consoleOpen}
        displayCurrency={displayCurrency}
        loading={loading}
        refreshAction={refreshAction}
        setActiveTab={setActiveTab}
        setConsoleOpen={setConsoleOpen}
        setDisplayCurrency={setDisplayCurrency}
        snapshot={snapshot}
        onRefresh={() => void handleRefresh()}
      />
      <main className="dashboard-main">
        {activeTab === "Portfolio" && <PortfolioTab period={period} setPeriod={setPeriod} snapshot={snapshot} />}
        {activeTab === "Maestro" && <MaestroTab openApp={openVirtuosoApp} snapshot={snapshot} />}
        {activeTab === "Virtuoso" && (
          <VirtuosoTab
            generatingStrategyId={signalAction.busy ? signalAction.strategyId : ""}
            onGenerateSignal={(strategyId) => void handleGenerateSignal(strategyId)}
            selectedStrategyId={selectedStrategyId}
            setSelectedStrategyId={setSelectedStrategyId}
            signalAction={signalAction.message ? signalAction : null}
            snapshot={snapshot}
          />
        )}
        {activeTab === "Research" && <ResearchTab snapshot={snapshot} />}
      </main>
      <footer className="statusbar">
        <span>{refreshAction.message}</span>
        <span>Read-only dashboard / proposal-only signals / no trading controls</span>
      </footer>
      <ConsoleDrawer
        copyState={copyState}
        diagnosticContext={diagnosticContext}
        open={consoleOpen}
        query={consoleQuery}
        selectedRunId={selectedRunId}
        setCopyState={setCopyState}
        setOpen={setConsoleOpen}
        setQuery={setConsoleQuery}
        setSelectedRunId={setSelectedRunId}
        snapshot={snapshot}
      />
    </div>
  );
}
