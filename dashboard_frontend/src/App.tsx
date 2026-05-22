import { useEffect, useMemo, useState } from "react";
import type { DashboardSnapshot, Metric, Row, Tone } from "./types";

const tabs = ["Overview", "Operations", "Portfolio", "Virtuoso", "Evidence", "Raw"] as const;

type TabName = (typeof tabs)[number];
type Theme = "System Default" | "Dark" | "Light";
type Filters = { query: string; status: string };

export function App() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [displayCurrency, setDisplayCurrency] = useState<"KRW" | "USD">("KRW");
  const [theme, setTheme] = useState<Theme>("System Default");
  const [activeTab, setActiveTab] = useState<TabName>("Overview");
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    document.documentElement.dataset.theme =
      theme === "System Default" ? "system" : theme.toLowerCase();
  }, [theme]);

  useEffect(() => {
    void loadSnapshot(displayCurrency, setSnapshot, setLoading, setError);
  }, [displayCurrency]);

  const filters = useMemo(() => ({ query, status: statusFilter }), [query, statusFilter]);
  const refresh = () => void loadSnapshot(displayCurrency, setSnapshot, setLoading, setError);

  if (loading && !snapshot) {
    return <ShellMessage title="Symphony" copy="Loading read-only dashboard state..." />;
  }

  if (error || !snapshot) {
    return (
      <ShellMessage
        title="Symphony"
        copy={error || "Dashboard snapshot is unavailable."}
        tone="danger"
      />
    );
  }

  return (
    <div className="app-shell">
      <Sidebar
        activeTab={activeTab}
        displayCurrency={displayCurrency}
        filters={filters}
        loading={loading}
        query={query}
        refresh={refresh}
        setActiveTab={setActiveTab}
        setDisplayCurrency={setDisplayCurrency}
        setQuery={setQuery}
        setStatusFilter={setStatusFilter}
        setTheme={setTheme}
        snapshot={snapshot}
        statusFilter={statusFilter}
        theme={theme}
      />

      <main className="content">
        <CommandCenter snapshot={snapshot} />
        {activeTab === "Overview" && <Overview snapshot={snapshot} filters={filters} />}
        {activeTab === "Operations" && <Operations snapshot={snapshot} filters={filters} />}
        {activeTab === "Portfolio" && <Portfolio snapshot={snapshot} filters={filters} />}
        {activeTab === "Virtuoso" && <Virtuoso snapshot={snapshot} filters={filters} />}
        {activeTab === "Evidence" && <Evidence snapshot={snapshot} filters={filters} />}
        {activeTab === "Raw" && (
          <section className="section">
            <SectionTitle eyebrow="Raw" title="Persisted Payloads" />
            <JsonBlock title="Raw System Status" value={snapshot.raw} defaultOpen />
          </section>
        )}
      </main>
    </div>
  );
}

function Sidebar({
  activeTab,
  displayCurrency,
  filters,
  loading,
  query,
  refresh,
  setActiveTab,
  setDisplayCurrency,
  setQuery,
  setStatusFilter,
  setTheme,
  snapshot,
  statusFilter,
  theme,
}: {
  activeTab: TabName;
  displayCurrency: "KRW" | "USD";
  filters: Filters;
  loading: boolean;
  query: string;
  refresh: () => void;
  setActiveTab: (tab: TabName) => void;
  setDisplayCurrency: (currency: "KRW" | "USD") => void;
  setQuery: (query: string) => void;
  setStatusFilter: (status: string) => void;
  setTheme: (theme: Theme) => void;
  snapshot: DashboardSnapshot;
  statusFilter: string;
  theme: Theme;
}) {
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <span>Symphony</span>
        <strong className={toneClass(snapshot.system_verdict.tone)}>
          {snapshot.system_verdict.title}
        </strong>
      </div>

      <nav className="side-nav" aria-label="Dashboard sections">
        {tabs.map((tab) => (
          <button
            key={tab}
            className={tab === activeTab ? "side-tab active" : "side-tab"}
            type="button"
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </button>
        ))}
      </nav>

      <div className="sidebar-group">
        <LabeledSelect
          label="Theme"
          value={theme}
          values={["System Default", "Dark", "Light"]}
          onChange={(value) => setTheme(value as Theme)}
        />
        <LabeledSelect
          label="Display currency"
          value={displayCurrency}
          values={["KRW", "USD"]}
          onChange={(value) => setDisplayCurrency(value as "KRW" | "USD")}
        />
        <button className="button primary" type="button" onClick={refresh} disabled={loading}>
          {loading ? "Refreshing" : "Refresh"}
        </button>
      </div>

      <div className="sidebar-group">
        <label className="field">
          <span>Search evidence</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} />
        </label>
        <label className="field">
          <span>Status filter</span>
          <input
            placeholder="fresh, stale, failed"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
          />
        </label>
        <span className="filter-count">
          {filters.query || filters.status ? "Filters active" : "No evidence filters"}
        </span>
      </div>

      <details className="sidebar-meta">
        <summary>Operator Paths</summary>
        <p>Config</p>
        <code>{snapshot.header.config_path}</code>
        <p>State</p>
        <code>{snapshot.header.state_path}</code>
        <p>Audit</p>
        <code>{snapshot.header.audit_path}</code>
      </details>
    </aside>
  );
}

function CommandCenter({ snapshot }: { snapshot: DashboardSnapshot }) {
  const fingerprint = snapshot.header.operator_config?.fingerprint || "none";
  const verdict = snapshot.system_verdict;
  const primaryStatus = verdict.status_metrics.slice(0, 4);

  return (
    <section className="command-center">
      <div className="command-header">
        <div>
          <div className="eyebrow">SYMPHONY / MAESTRO</div>
          <h1>Symphony</h1>
        </div>
        <div className="badge-row">
          <Badge label="Mode" value={snapshot.header.mode} tone="primary" />
          <Badge
            label="Orders"
            value={snapshot.header.order_posture}
            tone={snapshot.header.order_posture === "armed" ? "success" : "warning"}
          />
          <Badge label="Access" value="read-only" tone="success" />
          <Badge label="Config" value={fingerprint.slice(0, 12)} />
        </div>
      </div>

      <div className="command-grid">
        <StatusBanner title={verdict.title} copy={verdict.copy} tone={verdict.tone} />
        <MetricGrid metrics={primaryStatus} density="compact" />
      </div>

      <div className="capital-strip">
        <MetricGrid metrics={verdict.capital_summary} density="compact" />
      </div>
    </section>
  );
}

function Overview({ snapshot, filters }: { snapshot: DashboardSnapshot; filters: Filters }) {
  const map = snapshot.symphony_map;
  return (
    <section className="section">
      <SectionTitle eyebrow="Overview" title="System Map" />
      <MetricGrid metrics={map.metrics} />
      <div className="flow-grid">
        {map.nodes.map((node) => (
          <article className={`flow-card ${toneClass(node.tone)}`} key={String(node.title)}>
            <div className="flow-step">{formatValue(node.step)}</div>
            <h3>{formatValue(node.title)}</h3>
            <strong>{formatValue(node.status)}</strong>
            <p>{formatValue(node.detail)}</p>
          </article>
        ))}
      </div>
      <DataTable
        title="Attention Items"
        rows={map.attention_items}
        filters={filters}
        defaultOpen={map.attention_items.length > 0}
      />
      <DataTable title="Verdict Reasons" rows={map.verdict_reason_rows} filters={filters} />
      <DataTable title="Asset Currency Summary" rows={map.asset_summary_rows} filters={filters} />
      <DataTable title="Freshness" rows={map.freshness} filters={filters} />
      <DataTable title="Run Index" rows={map.run_index} filters={filters} />
    </section>
  );
}

function Operations({ snapshot, filters }: { snapshot: DashboardSnapshot; filters: Filters }) {
  const cockpit = snapshot.operator_cockpit;
  return (
    <section className="section">
      <SectionTitle eyebrow="Operations" title="Operator Cockpit" />
      <MetricGrid metrics={cockpit.metrics} />
      <DataTable
        title="Attention Items"
        rows={cockpit.attention_items}
        filters={filters}
        defaultOpen={cockpit.attention_items.length > 0}
      />
      <DataTable title="Health Checks" rows={cockpit.health_checks} filters={filters} defaultOpen />
      <DataTable title="Freshness" rows={cockpit.freshness} filters={filters} />
      <DataTable
        title="Live Order Lifecycle"
        rows={asRows(cockpit.live_order_lifecycle.recent)}
        filters={filters}
      />
      <DataTable title="Recent Risk Decisions" rows={cockpit.risk_decisions} filters={filters} />
      <DataTable
        title="Recent Halt / Failure Events"
        rows={cockpit.halt_failure_events}
        filters={filters}
      />
      <JsonBlock title="Operator Summary Payload" value={cockpit.operator_summary} />
    </section>
  );
}

function Portfolio({ snapshot, filters }: { snapshot: DashboardSnapshot; filters: Filters }) {
  const investment = snapshot.investment_console;
  return (
    <section className="section">
      <SectionTitle eyebrow="Portfolio" title="Investment Console" />
      <MetricGrid metrics={investment.metrics} />
      <MetricGrid metrics={investment.asset_summary_metrics} density="compact" />
      <div className="chart-grid-layout">
        <LineChart
          title="Account Value"
          rows={investment.account_performance}
          xKey="created_at"
          yKey="total_value"
        />
        <LineChart
          title="Total Portfolio Value"
          rows={investment.total_portfolio_performance}
          xKey="created_at"
          yKey="total_value"
        />
      </div>
      <DataTable
        title="Asset Currency Summary"
        rows={investment.asset_summary_rows}
        filters={filters}
        defaultOpen
      />
      <DataTable
        title="Strategy Attribution"
        rows={investment.strategy_attribution}
        filters={filters}
        defaultOpen={investment.strategy_attribution.length > 0}
      />
      <DataTable
        title="Account Performance"
        rows={investment.account_performance}
        filters={filters}
      />
      <DataTable
        title="Total Portfolio Performance"
        rows={investment.total_portfolio_performance}
        filters={filters}
      />
      <DataTable
        title="Currency Sleeve Performance"
        rows={investment.currency_sleeve_performance}
        filters={filters}
      />
      <DataTable
        title="Strategy Book Performance"
        rows={investment.strategy_book_performance}
        filters={filters}
      />
      <DataTable title="Broker Position Exposure" rows={investment.broker_positions} filters={filters} />
      <DataTable title="Maestro State Exposure" rows={investment.maestro_exposure} filters={filters} />
      <DataTable title="Portfolio" rows={investment.portfolio} filters={filters} />
      <div className="split">
        <JsonBlock title="Latest Broker Account" value={investment.broker_summary} />
        <JsonBlock
          title="Broker / Reconciliation"
          value={{
            broker_snapshot: investment.broker_snapshot,
            reconciliation: investment.reconciliation,
          }}
        />
      </div>
    </section>
  );
}

function Virtuoso({ snapshot, filters }: { snapshot: DashboardSnapshot; filters: Filters }) {
  const [activeStrategy, setActiveStrategy] = useState("");
  const apps = snapshot.virtuoso_apps;
  const selected =
    apps.strategies.find((strategy) => strategy.strategy_id === activeStrategy) ||
    apps.strategies[0];

  useEffect(() => {
    if (!activeStrategy && apps.strategies[0]) {
      setActiveStrategy(apps.strategies[0].strategy_id);
    }
  }, [activeStrategy, apps.strategies]);

  return (
    <section className="section">
      <SectionTitle eyebrow="Virtuoso" title="Strategy Apps" />
      <MetricGrid metrics={apps.metrics} />
      <DataTable title="Virtuoso Strategy Overview" rows={apps.overview} filters={filters} defaultOpen />
      {!selected ? (
        <StatusBanner
          title="No Virtuoso strategies"
          copy="No configured or persisted strategy app state was found."
          tone="warning"
        />
      ) : (
        <>
          <div className="strategy-tabs">
            {apps.strategies.map((strategy) => (
              <button
                key={strategy.strategy_id}
                className={strategy.strategy_id === selected.strategy_id ? "tab active" : "tab"}
                type="button"
                onClick={() => setActiveStrategy(strategy.strategy_id)}
              >
                {strategy.strategy_id}
              </button>
            ))}
          </div>
          <MetricGrid metrics={strategyMetrics(selected)} />
          <div className="split">
            <DataTable title="App Concept" rows={selected.concept} filters={filters} defaultOpen />
            <DataTable title="Operation State" rows={selected.operation} filters={filters} defaultOpen />
          </div>
          <LineChart
            title="Strategy Book Return"
            rows={selected.performance}
            xKey="created_at"
            yKey="cumulative_return"
          />
          <DataTable title="Strategy Book Returns" rows={selected.performance} filters={filters} />
          <DataTable title="Strategy Attribution" rows={selected.attribution} filters={filters} />
          <DataTable title="Strategy Book Snapshots" rows={selected.snapshots} filters={filters} />
          <DataTable title="Recent Strategy Runs" rows={selected.runs} filters={filters} />
          <JsonBlock title="Virtuoso App Config" value={selected.config} />
        </>
      )}
    </section>
  );
}

function Evidence({ snapshot, filters }: { snapshot: DashboardSnapshot; filters: Filters }) {
  const audit = snapshot.audit_trail;
  const [selectedRunId, setSelectedRunId] = useState("");
  const [runDetail, setRunDetail] = useState<Row | null>(null);

  useEffect(() => {
    const runId = selectedRunId || String(audit.run_index[0]?.run_id || "");
    if (!runId) {
      return;
    }
    setSelectedRunId(runId);
    fetch(`/api/dashboard/runs/${encodeURIComponent(runId)}`)
      .then((response) => response.json())
      .then((payload) => setRunDetail(payload as Row))
      .catch(() => setRunDetail(null));
  }, [selectedRunId, audit.run_index]);

  return (
    <section className="section">
      <SectionTitle eyebrow="Evidence" title="Audit Trail" />
      <MetricGrid metrics={audit.metrics} />
      <DataTable
        title="Strategy Signals / Results"
        rows={audit.strategy_signal_rows}
        filters={filters}
        defaultOpen={audit.strategy_signal_rows.length > 0}
      />
      <DataTable title="Recent Paper Orders" rows={audit.orders} filters={filters} />
      <DataTable title="Recent Approvals" rows={audit.approvals} filters={filters} />
      <label className="field inline run-selector">
        <span>Run Detail</span>
        <select value={selectedRunId} onChange={(event) => setSelectedRunId(event.target.value)}>
          {audit.run_index.map((row) => (
            <option key={String(row.run_id)} value={String(row.run_id)}>
              {String(row.run_id)}
            </option>
          ))}
        </select>
      </label>
      <JsonBlock title="Selected Run Detail" value={runDetail} />
      <DataTable title="Run Index" rows={audit.run_index} filters={filters} />
      <DataTable title="Recent Broker Account Snapshots" rows={audit.broker_snapshots} filters={filters} />
      <DataTable title="Live Order Status / Lifecycle Events" rows={audit.live_order_events} filters={filters} />
      <DataTable title="Fill Reconciliation Events" rows={audit.fill_reconciliation} filters={filters} />
      <DataTable title="Recent System Events" rows={audit.system_events} filters={filters} />
    </section>
  );
}

function MetricGrid({
  metrics,
  density = "normal",
}: {
  metrics: Metric[];
  density?: "normal" | "compact";
}) {
  return (
    <div className={`metric-grid ${density === "compact" ? "compact" : ""}`}>
      {metrics.map((metric) => (
        <article className={`metric ${toneClass(metric.tone)}`} key={metric.label}>
          <span>{metric.label}</span>
          <strong>{formatValue(metric.value)}</strong>
        </article>
      ))}
    </div>
  );
}

function DataTable({
  title,
  rows,
  filters,
  defaultOpen = false,
}: {
  title: string;
  rows: Row[];
  filters: Filters;
  defaultOpen?: boolean;
}) {
  const filteredRows = filterRows(rows, filters);
  const columns = columnsFor(filteredRows.length ? filteredRows : rows);
  return (
    <details className="evidence-panel" open={defaultOpen}>
      <summary className="evidence-summary">
        <span>
          <strong>{title}</strong>
          <small>
            {filteredRows.length} / {rows.length} rows
          </small>
        </span>
        <button
          type="button"
          className="button"
          disabled={!filteredRows.length}
          onClick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            downloadCsv(title, filteredRows);
          }}
        >
          CSV
        </button>
      </summary>
      {!filteredRows.length ? (
        <div className="empty">No rows</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {columns.map((column) => (
                  <th key={column}>{column}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filteredRows.slice(0, 250).map((row, index) => (
                <tr key={index}>
                  {columns.map((column) => (
                    <td key={column}>{formatValue(row[column])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </details>
  );
}

function LineChart({
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
    return null;
  }
  const path = points
    .map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`)
    .join(" ");
  return (
    <section className="chart-card">
      <div className="panel-title">
        <strong>{title}</strong>
        <span>
          {formatValue(rows[rows.length - 1]?.[xKey])} to {formatValue(rows[0]?.[xKey])}
        </span>
      </div>
      <svg viewBox="0 0 640 180" role="img" aria-label={title}>
        <path className="chart-grid" d="M 20 40 H 620 M 20 90 H 620 M 20 140 H 620" />
        <path className="chart-line" d={path} />
      </svg>
    </section>
  );
}

function SectionTitle({ eyebrow, title }: { eyebrow: string; title: string }) {
  return (
    <header className="section-title">
      <span>{eyebrow}</span>
      <h2>{title}</h2>
    </header>
  );
}

function StatusBanner({ title, copy, tone }: { title: string; copy: string; tone: Tone }) {
  return (
    <article className={`banner ${toneClass(tone)}`}>
      <strong>{title}</strong>
      <p>{copy}</p>
    </article>
  );
}

function JsonBlock({
  title,
  value,
  defaultOpen = false,
}: {
  title: string;
  value: unknown;
  defaultOpen?: boolean;
}) {
  return (
    <details className="evidence-panel json-card" open={defaultOpen}>
      <summary className="evidence-summary">
        <span>
          <strong>{title}</strong>
          <small>JSON</small>
        </span>
      </summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

function Badge({ label, value, tone = "neutral" }: { label: string; value: unknown; tone?: Tone }) {
  return (
    <span className={`badge ${toneClass(tone)}`}>
      {label} <strong>{formatValue(value)}</strong>
    </span>
  );
}

function LabeledSelect({
  label,
  value,
  values,
  onChange,
}: {
  label: string;
  value: string;
  values: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {values.map((item) => (
          <option key={item} value={item}>
            {item}
          </option>
        ))}
      </select>
    </label>
  );
}

function ShellMessage({ title, copy, tone = "neutral" }: { title: string; copy: string; tone?: Tone }) {
  return (
    <main className="center-shell">
      <StatusBanner title={title} copy={copy} tone={tone} />
    </main>
  );
}

async function loadSnapshot(
  currency: "KRW" | "USD",
  setSnapshot: (snapshot: DashboardSnapshot) => void,
  setLoading: (loading: boolean) => void,
  setError: (error: string | null) => void,
) {
  setLoading(true);
  setError(null);
  try {
    const response = await fetch(`/api/dashboard/snapshot?display_currency=${currency}`);
    if (!response.ok) {
      throw new Error(`Snapshot request failed: ${response.status}`);
    }
    setSnapshot((await response.json()) as DashboardSnapshot);
  } catch (error) {
    setError(error instanceof Error ? error.message : "Unknown dashboard error");
  } finally {
    setLoading(false);
  }
}

function filterRows(rows: Row[], filters: Filters): Row[] {
  const query = filters.query.trim().toLowerCase();
  const status = filters.status.trim().toLowerCase();
  return rows.filter((row) => {
    const haystack = JSON.stringify(row).toLowerCase();
    const queryMatches = !query || haystack.includes(query);
    const statusMatches =
      !status || statusCandidates(row).some((candidate) => candidate.includes(status));
    return queryMatches && statusMatches;
  });
}

function statusCandidates(row: Row): string[] {
  return ["status", "reconciliation_status", "state", "approved", "validation_ok", "passed", "fx_status"]
    .map((key) => row[key])
    .filter((value) => value !== undefined && value !== null)
    .map((value) => String(value).toLowerCase());
}

function columnsFor(rows: Row[]): string[] {
  return Array.from(new Set(rows.flatMap((row) => Object.keys(row)))).slice(0, 16);
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "n/a";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

function toneClass(tone: unknown): string {
  const normalized = String(tone || "neutral").toLowerCase();
  if (["success", "ok", "fresh", "passed", "approved", "active"].includes(normalized)) {
    return "tone-success";
  }
  if (["warning", "warn", "stale", "missing", "open"].includes(normalized)) {
    return "tone-warning";
  }
  if (["danger", "fail", "failed", "error", "halted", "rejected"].includes(normalized)) {
    return "tone-danger";
  }
  if (normalized === "primary") {
    return "tone-primary";
  }
  return "";
}

function asRows(value: unknown): Row[] {
  return Array.isArray(value) ? (value as Row[]) : [];
}

function strategyMetrics(strategy: DashboardSnapshot["virtuoso_apps"]["strategies"][number]): Metric[] {
  const enabled = enabledValue(strategy.operation);
  return [
    { label: "State", value: enabled, tone: enabled === "true" ? "success" : "warning" },
    { label: "Latest Run", value: strategy.runs[0]?.created_at || "n/a" },
    { label: "Book Value", value: strategy.summary.book_value ?? "n/a" },
    { label: "Cumulative Return", value: strategy.summary.cumulative_return ?? "n/a" },
    { label: "Drawdown", value: strategy.summary.drawdown ?? "n/a", tone: "warning" },
  ];
}

function enabledValue(operation: Row[]): string {
  const row = operation.find((item) => item.item === "Enabled");
  return String(row?.value ?? "false");
}

function chartPoints(rows: Row[], key: string) {
  const values = rows
    .slice()
    .reverse()
    .map((row) => Number(row[key]))
    .filter((value) => Number.isFinite(value));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  return values.map((value, index) => ({
    x: 20 + (index / Math.max(values.length - 1, 1)) * 600,
    y: 160 - ((value - min) / range) * 140,
  }));
}

function downloadCsv(title: string, rows: Row[]) {
  const columns = columnsFor(rows);
  const body = [
    columns.join(","),
    ...rows.map((row) =>
      columns.map((column) => `"${formatValue(row[column]).replaceAll("\"", "\"\"")}"`).join(","),
    ),
  ].join("\n");
  const url = URL.createObjectURL(new Blob([body], { type: "text/csv" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${title.toLowerCase().replaceAll(/[^a-z0-9]+/g, "_")}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}
