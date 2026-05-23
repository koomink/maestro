import { useEffect, useMemo, useState } from "react";
import type { DashboardSnapshot, Metric, Row, Tone } from "./types";

const tabs = ["Daily Brief", "Analysis Report", "Virtuoso"] as const;
const periods = ["7D", "30D", "90D", "All"] as const;

type TabName = (typeof tabs)[number];
type Period = (typeof periods)[number];

export function App() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [displayCurrency, setDisplayCurrency] = useState<"KRW" | "USD">("KRW");
  const [activeTab, setActiveTab] = useState<TabName>("Daily Brief");
  const [period, setPeriod] = useState<Period>("30D");
  const [consoleOpen, setConsoleOpen] = useState(false);
  const [consoleQuery, setConsoleQuery] = useState("");
  const [selectedRunId, setSelectedRunId] = useState("");
  const [selectedStrategyId, setSelectedStrategyId] = useState("");
  const [copyState, setCopyState] = useState("Copy diagnostic context");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

  useEffect(() => {
    if (!snapshot || selectedStrategyId) {
      return;
    }
    const firstStrategyId = snapshot.virtuoso_apps.strategies[0]?.strategy_id || "";
    if (firstStrategyId) {
      setSelectedStrategyId(firstStrategyId);
    }
  }, [selectedStrategyId, snapshot]);

  if (loading && !snapshot) {
    return <ShellMessage title="Symphony Maestro" copy="Loading the editorial dashboard brief..." />;
  }

  if (error || !snapshot) {
    return (
      <ShellMessage
        title="Symphony Maestro"
        copy={error || "Dashboard snapshot is unavailable."}
        tone="danger"
      />
    );
  }

  const trust = trustSummary(snapshot);
  const selectedStrategy =
    snapshot.virtuoso_apps.strategies.find((strategy) => strategy.strategy_id === selectedStrategyId) ||
    snapshot.virtuoso_apps.strategies[0];

  const diagnosticContext = buildDiagnosticContext(
    snapshot,
    activeTab,
    displayCurrency,
    period,
    selectedRunId,
    selectedStrategy?.strategy_id || "",
  );

  return (
    <div className={consoleOpen ? "app-shell console-is-open" : "app-shell"}>
      <header className="topbar">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true">
            *
          </span>
          <div>
            <strong>Symphony Maestro</strong>
            <span>Read-only portfolio intelligence</span>
          </div>
        </div>

        <nav className="tab-list" aria-label="Dashboard sections">
          {tabs.map((tab) => (
            <button
              key={tab}
              className={tab === activeTab ? "tab-button active" : "tab-button"}
              type="button"
              onClick={() => setActiveTab(tab)}
            >
              {tab}
            </button>
          ))}
        </nav>

        <div className="top-actions">
          <SegmentedControl
            label="Display currency"
            value={displayCurrency}
            values={["KRW", "USD"]}
            onChange={(value) => setDisplayCurrency(value as "KRW" | "USD")}
          />
          <button className="button secondary" type="button" onClick={() => void loadSnapshot(displayCurrency, setSnapshot, setLoading, setError)}>
            {loading ? "Refreshing" : "Refresh"}
          </button>
          <button className="button primary" type="button" onClick={() => setConsoleOpen((open) => !open)}>
            {consoleOpen ? "Close Console" : "Open Console"}
          </button>
        </div>
      </header>

      <main className="page">
        <TrustStrip snapshot={snapshot} trust={trust} />
        {activeTab === "Daily Brief" && (
          <DailyBrief snapshot={snapshot} trust={trust} openConsole={() => setConsoleOpen(true)} />
        )}
        {activeTab === "Analysis Report" && (
          <AnalysisReport
            period={period}
            setPeriod={setPeriod}
            snapshot={snapshot}
          />
        )}
        {activeTab === "Virtuoso" && (
          <VirtuosoReport
            selectedStrategyId={selectedStrategy?.strategy_id || ""}
            setSelectedStrategyId={setSelectedStrategyId}
            snapshot={snapshot}
          />
        )}
      </main>

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
        trust={trust}
      />
    </div>
  );
}

function DailyBrief({
  snapshot,
  trust,
  openConsole,
}: {
  snapshot: DashboardSnapshot;
  trust: TrustSummary;
  openConsole: () => void;
}) {
  const investment = snapshot.investment_console;
  const totalRows = investment.total_portfolio_performance;
  const latestTotal = totalRows[0] || {};
  const previousTotal = totalRows[1] || {};
  const totalValue = firstValue(latestTotal, ["total_value", "ending_value", "market_value", "value"]);
  const totalReturn = firstValue(latestTotal, ["cumulative_return", "return", "daily_return", "pnl_pct"]);
  const dailyChange = firstValue(latestTotal, ["daily_pnl", "pnl", "change", "return_pct"]);
  const previousValue = firstValue(previousTotal, ["total_value", "ending_value", "market_value", "value"]);

  return (
    <section className="report-stack">
      <section className="hero-report">
        <div className="hero-copy">
          <span className="eyebrow">Daily Brief</span>
          <h1>Portfolio first, evidence close.</h1>
          <p>
            A warm read-only brief for portfolio value, currency sleeves, and the trust signals behind
            today&apos;s state.
          </p>
          <div className="hero-actions">
            <button className="button primary" type="button" onClick={openConsole}>
              Investigate in Console
            </button>
            <span className={`status-line ${toneClass(snapshot.system_verdict.tone)}`}>
              {snapshot.system_verdict.title}
            </span>
          </div>
        </div>

        <article className="portfolio-marquee">
          <span>Total portfolio</span>
          <strong>{formatValue(totalValue || snapshot.system_verdict.capital_summary[0]?.value)}</strong>
          <div className="marquee-grid">
            <MiniFact label="Display" value={snapshot.display_currency} />
            <MiniFact label="Latest return" value={totalReturn || "n/a"} />
            <MiniFact label="Daily change" value={dailyChange || "n/a"} />
            <MiniFact label="Previous value" value={previousValue || "n/a"} />
          </div>
        </article>
      </section>

      <section className="brief-grid">
        <Panel title="Capital Summary" eyebrow="Portfolio">
          <MetricGrid metrics={snapshot.system_verdict.capital_summary} featured />
        </Panel>
        <Panel title="Currency Sleeves" eyebrow="KRW / USD">
          <ReadableTable rows={investment.asset_summary_rows} limit={5} />
        </Panel>
      </section>

      <section className="brief-grid align-start">
        <Panel title="Trust Signals" eyebrow="Can I trust it?">
          <div className="signal-list">
            <Signal label="Freshness" value={trust.freshness} tone={trust.freshnessTone} />
            <Signal label="Reconciliation" value={trust.reconciliation} tone={trust.reconciliationTone} />
            <Signal label="FX Status" value={trust.fxStatus} tone={trust.fxTone} />
            <Signal label="Latest Snapshot" value={trust.latestSnapshot} tone="neutral" />
          </div>
        </Panel>
        <Panel title="Attention" eyebrow="What needs a look?">
          <ReadableList
            empty="No attention items in the current snapshot."
            rows={snapshot.symphony_map.attention_items}
            titleKeys={["source", "title", "name", "status"]}
            detailKeys={["reason", "detail", "message"]}
            limit={5}
          />
        </Panel>
      </section>

      <section className="brief-grid align-start">
        <Panel title="Broker Truth" eyebrow="Account">
          <KeyValueRows row={investment.broker_summary} keys={["status", "account_id", "total_value", "cash", "positions", "fetched_at"]} />
        </Panel>
        <Panel title="Reconciliation" eyebrow="State">
          <KeyValueRows row={investment.reconciliation} keys={["status", "passed", "created_at", "issue_count", "message"]} />
        </Panel>
      </section>
    </section>
  );
}

function AnalysisReport({
  period,
  setPeriod,
  snapshot,
}: {
  period: Period;
  setPeriod: (period: Period) => void;
  snapshot: DashboardSnapshot;
}) {
  const investment = snapshot.investment_console;
  const totalPerformance = filterByPeriod(investment.total_portfolio_performance, period);
  const accountPerformance = filterByPeriod(investment.account_performance, period);
  const sleevePerformance = filterByPeriod(investment.currency_sleeve_performance, period);
  const strategyPerformance = filterByPeriod(investment.strategy_book_performance, period);

  return (
    <section className="report-stack">
      <SectionHeader
        eyebrow="Analysis Report"
        title="What changed, and what drove it?"
        copy="A report-style view for total portfolio performance, currency sleeve behavior, and strategy contribution. Period controls are frontend-local for v1 and ready for a future backend period API."
      >
        <SegmentedControl
          label="Period"
          value={period}
          values={[...periods]}
          onChange={(value) => setPeriod(value as Period)}
        />
      </SectionHeader>

      <section className="analysis-grid">
        <LineChart title="Total Portfolio Value" rows={totalPerformance} xKey="created_at" yKey="total_value" />
        <LineChart title="Account Value" rows={accountPerformance} xKey="created_at" yKey="total_value" />
      </section>

      <section className="analysis-grid">
        <Panel title="Performance Markers" eyebrow={period}>
          <MetricGrid metrics={investment.metrics} />
        </Panel>
        <Panel title="FX & Conversion Context" eyebrow="Reporting">
          <KeyValueRows row={investment.fx_snapshot} keys={["status", "source", "rate", "as_of", "created_at"]} />
        </Panel>
      </section>

      <section className="analysis-grid wide-first">
        <Panel title="Strategy Attribution" eyebrow="Contribution">
          <ReadableTable rows={investment.strategy_attribution} limit={8} />
        </Panel>
        <Panel title="Currency Sleeve Performance" eyebrow={period}>
          <ReadableTable rows={sleevePerformance} limit={8} />
        </Panel>
      </section>

      <section className="analysis-grid">
        <Panel title="Total Portfolio Performance" eyebrow={period}>
          <ReadableTable rows={totalPerformance} limit={8} />
        </Panel>
        <Panel title="Strategy Book Performance" eyebrow={period}>
          <ReadableTable rows={strategyPerformance} limit={8} />
        </Panel>
      </section>
    </section>
  );
}

function VirtuosoReport({
  selectedStrategyId,
  setSelectedStrategyId,
  snapshot,
}: {
  selectedStrategyId: string;
  setSelectedStrategyId: (strategyId: string) => void;
  snapshot: DashboardSnapshot;
}) {
  const apps = snapshot.virtuoso_apps;
  const selected =
    apps.strategies.find((strategy) => strategy.strategy_id === selectedStrategyId) ||
    apps.strategies[0];

  return (
    <section className="report-stack">
      <SectionHeader
        eyebrow="Virtuoso"
        title="What did each app do?"
        copy="App-level activity, health, recent runs, and book performance without embedding strategy-specific logic in Maestro."
      />

      <section className="virtuoso-layout">
        <aside className="app-list" aria-label="Virtuoso apps">
          <MetricGrid metrics={apps.metrics} />
          {apps.strategies.map((strategy) => (
            <button
              key={strategy.strategy_id}
              className={strategy.strategy_id === selected?.strategy_id ? "app-card active" : "app-card"}
              type="button"
              onClick={() => setSelectedStrategyId(strategy.strategy_id)}
            >
              <span>{strategy.strategy_id}</span>
              <strong>{formatValue(strategy.summary.cumulative_return ?? strategy.summary.book_value ?? "n/a")}</strong>
              <small>{formatValue(strategy.runs[0]?.created_at || "No recent run")}</small>
            </button>
          ))}
        </aside>

        {!selected ? (
          <Panel title="No Virtuoso apps" eyebrow="Empty">
            <p className="muted-copy">No configured or persisted strategy app state was found.</p>
          </Panel>
        ) : (
          <section className="app-detail">
            <div className="dark-feature">
              <span className="eyebrow">Selected App</span>
              <h2>{selected.strategy_id}</h2>
              <MetricGrid metrics={strategyMetrics(selected)} featured />
            </div>

            <section className="analysis-grid">
              <Panel title="Concept" eyebrow="App shape">
                <ReadableTable rows={selected.concept} limit={6} />
              </Panel>
              <Panel title="Operation State" eyebrow="Current">
                <ReadableTable rows={selected.operation} limit={6} />
              </Panel>
            </section>

            <LineChart
              title="Strategy Book Return"
              rows={selected.performance}
              xKey="created_at"
              yKey="cumulative_return"
            />

            <section className="analysis-grid">
              <Panel title="Recent Runs" eyebrow="Activity">
                <ReadableTable rows={selected.runs} limit={8} />
              </Panel>
              <Panel title="Attribution" eyebrow="Contribution">
                <ReadableTable rows={selected.attribution} limit={8} />
              </Panel>
            </section>
          </section>
        )}
      </section>
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
  trust,
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
  trust: TrustSummary;
}) {
  const evidenceRows = evidenceSummaries(snapshot, query);

  async function copyDiagnosticContext() {
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
      <div className="console-header">
        <div>
          <span className="eyebrow">Console</span>
          <h2>Evidence without raw logs</h2>
        </div>
        <button className="icon-button" type="button" onClick={() => setOpen(false)} aria-label="Close console">
          x
        </button>
      </div>

      <section className="console-section">
        <h3>Status</h3>
        <Signal label="Verdict" value={snapshot.system_verdict.title} tone={snapshot.system_verdict.tone} />
        <Signal label="Freshness" value={trust.freshness} tone={trust.freshnessTone} />
        <Signal label="Reconciliation" value={trust.reconciliation} tone={trust.reconciliationTone} />
        <Signal label="Latest Snapshot" value={trust.latestSnapshot} tone="neutral" />
      </section>

      <section className="console-section">
        <h3>Find Evidence</h3>
        <label className="field">
          <span>Search summaries</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="run, stale, broker, approval"
          />
        </label>
        <label className="field">
          <span>Run context</span>
          <select value={selectedRunId} onChange={(event) => setSelectedRunId(event.target.value)}>
            {snapshot.audit_trail.run_index.map((row) => (
              <option key={String(row.run_id)} value={String(row.run_id)}>
                {String(row.run_id)}
              </option>
            ))}
          </select>
        </label>
        <button className="button primary wide" type="button" onClick={() => void copyDiagnosticContext()}>
          {copyState}
        </button>
      </section>

      <section className="console-section">
        <h3>Operations Quick Check</h3>
        <ReadableList
          empty="No current operational attention."
          rows={[
            ...snapshot.operator_cockpit.attention_items,
            ...snapshot.operator_cockpit.health_checks.slice(0, 4),
            ...snapshot.operator_cockpit.halt_failure_events.slice(0, 3),
          ]}
          titleKeys={["source", "name", "check", "status", "event_type"]}
          detailKeys={["reason", "message", "detail", "created_at"]}
          limit={8}
        />
      </section>

      <section className="console-section">
        <h3>Evidence Summaries</h3>
        <ReadableList
          empty="No matching evidence summaries."
          rows={evidenceRows}
          titleKeys={["title", "source", "run_id"]}
          detailKeys={["detail", "created_at", "status"]}
          limit={10}
        />
      </section>
    </aside>
  );
}

function TrustStrip({ snapshot, trust }: { snapshot: DashboardSnapshot; trust: TrustSummary }) {
  const fingerprint = snapshot.header.operator_config?.fingerprint || "none";
  return (
    <section className="trust-strip" aria-label="Trust strip">
      <MiniFact label="Read-only" value={snapshot.read_only ? "yes" : "no"} tone="success" />
      <MiniFact label="Mode" value={snapshot.header.mode} />
      <MiniFact label="Orders" value={snapshot.header.order_posture} tone={snapshot.header.order_posture === "armed" ? "warning" : "neutral"} />
      <MiniFact label="Currency" value={snapshot.display_currency} />
      <MiniFact label="Freshness" value={trust.freshness} tone={trust.freshnessTone} />
      <MiniFact label="Reconciliation" value={trust.reconciliation} tone={trust.reconciliationTone} />
      <MiniFact label="Config" value={fingerprint.slice(0, 12)} />
    </section>
  );
}

function SectionHeader({
  eyebrow,
  title,
  copy,
  children,
}: {
  eyebrow: string;
  title: string;
  copy: string;
  children?: React.ReactNode;
}) {
  return (
    <header className="section-header">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{copy}</p>
      </div>
      {children ? <div className="section-controls">{children}</div> : null}
    </header>
  );
}

function Panel({
  title,
  eyebrow,
  children,
}: {
  title: string;
  eyebrow: string;
  children: React.ReactNode;
}) {
  return (
    <article className="panel">
      <span className="eyebrow">{eyebrow}</span>
      <h2>{title}</h2>
      {children}
    </article>
  );
}

function MetricGrid({ metrics, featured = false }: { metrics: Metric[]; featured?: boolean }) {
  return (
    <div className={featured ? "metric-grid featured" : "metric-grid"}>
      {metrics.map((metric) => (
        <article className={`metric ${toneClass(metric.tone)}`} key={metric.label}>
          <span>{metric.label}</span>
          <strong>{formatValue(metric.value)}</strong>
        </article>
      ))}
    </div>
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
    return (
      <Panel title={title} eyebrow="Chart">
        <p className="muted-copy">Not enough numeric history to draw this chart.</p>
      </Panel>
    );
  }
  const path = points
    .map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`)
    .join(" ");
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
        <path className="chart-grid" d="M 28 42 H 612 M 28 95 H 612 M 28 148 H 612 M 28 200 H 612" />
        <path className="chart-line-shadow" d={path} />
        <path className="chart-line" d={path} />
      </svg>
    </article>
  );
}

function ReadableTable({ rows, limit = 6 }: { rows: Row[]; limit?: number }) {
  if (!rows.length) {
    return <p className="muted-copy">No rows available.</p>;
  }
  const columns = columnsFor(rows).slice(0, 6);
  return (
    <div className="readable-table-wrap">
      <table className="readable-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{humanize(column)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, limit).map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={column}>{formatReadableCell(row[column])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ReadableList({
  empty,
  rows,
  titleKeys,
  detailKeys,
  limit,
}: {
  empty: string;
  rows: Row[];
  titleKeys: string[];
  detailKeys: string[];
  limit: number;
}) {
  if (!rows.length) {
    return <p className="muted-copy">{empty}</p>;
  }
  return (
    <div className="readable-list">
      {rows.slice(0, limit).map((row, index) => (
        <article className={`list-item ${toneClass(firstValue(row, ["tone", "status", "state"]))}`} key={index}>
          <strong>{formatValue(firstValue(row, titleKeys) || `Item ${index + 1}`)}</strong>
          <span>{formatValue(firstValue(row, detailKeys) || "No detail supplied.")}</span>
        </article>
      ))}
    </div>
  );
}

function KeyValueRows({ row, keys }: { row: Row; keys: string[] }) {
  const visible = keys
    .map((key) => [key, row[key]] as const)
    .filter(([, value]) => value !== undefined && value !== null);
  if (!visible.length) {
    return <p className="muted-copy">No summary available.</p>;
  }
  return (
    <dl className="key-values">
      {visible.map(([key, value]) => (
        <div key={key}>
          <dt>{humanize(key)}</dt>
          <dd>{formatReadableCell(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Signal({ label, value, tone }: { label: string; value: unknown; tone: Tone }) {
  return (
    <div className={`signal ${toneClass(tone)}`}>
      <span>{label}</span>
      <strong>{formatValue(value)}</strong>
    </div>
  );
}

function MiniFact({ label, value, tone = "neutral" }: { label: string; value: unknown; tone?: Tone }) {
  return (
    <span className={`mini-fact ${toneClass(tone)}`}>
      <span>{label}</span>
      <strong>{formatValue(value)}</strong>
    </span>
  );
}

function SegmentedControl({
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
    <div className="segmented" aria-label={label}>
      {values.map((item) => (
        <button
          key={item}
          className={item === value ? "active" : ""}
          type="button"
          onClick={() => onChange(item)}
        >
          {item}
        </button>
      ))}
    </div>
  );
}

function ShellMessage({ title, copy, tone = "neutral" }: { title: string; copy: string; tone?: Tone }) {
  return (
    <main className="center-shell">
      <article className={`shell-card ${toneClass(tone)}`}>
        <span className="eyebrow">Symphony</span>
        <h1>{title}</h1>
        <p>{copy}</p>
      </article>
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
      throw new Error(await dashboardErrorMessage(response));
    }
    setSnapshot((await response.json()) as DashboardSnapshot);
  } catch (error) {
    setError(error instanceof Error ? error.message : "Unknown dashboard error");
  } finally {
    setLoading(false);
  }
}

async function dashboardErrorMessage(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: string | { message?: string } };
    if (typeof payload.detail === "string") {
      return payload.detail;
    }
    if (payload.detail?.message) {
      return payload.detail.message;
    }
  } catch {
    // Keep the fallback below for non-JSON server errors.
  }
  return `Dashboard snapshot is unavailable (${response.status}).`;
}

type TrustSummary = {
  freshness: string;
  freshnessTone: Tone;
  reconciliation: string;
  reconciliationTone: Tone;
  fxStatus: string;
  fxTone: Tone;
  latestSnapshot: string;
};

function trustSummary(snapshot: DashboardSnapshot): TrustSummary {
  const freshnessMetric = snapshot.symphony_map.metrics.find((metric) => metric.label === "Freshness");
  const reconciliationMetric = snapshot.symphony_map.metrics.find((metric) => metric.label === "Reconciliation");
  const brokerSnapshot = snapshot.investment_console.broker_snapshot;
  const fxSnapshot = snapshot.investment_console.fx_snapshot;
  const latestSnapshot = firstValue(brokerSnapshot, ["fetched_at", "created_at", "as_of", "timestamp"]) || "n/a";
  const fxStatus = firstValue(fxSnapshot, ["status", "fx_status", "state"]) || "n/a";

  return {
    freshness: formatValue(freshnessMetric?.value || "n/a"),
    freshnessTone: freshnessMetric?.tone || "neutral",
    reconciliation: formatValue(reconciliationMetric?.value || "n/a"),
    reconciliationTone: reconciliationMetric?.tone || "neutral",
    fxStatus: formatValue(fxStatus),
    fxTone: toneFromValue(fxStatus),
    latestSnapshot: formatValue(latestSnapshot),
  };
}

function evidenceSummaries(snapshot: DashboardSnapshot, query: string): Row[] {
  const rows: Row[] = [
    ...snapshot.audit_trail.run_index.map((row) => ({
      title: "Run",
      source: "Run Index",
      run_id: row.run_id,
      created_at: row.created_at,
      status: firstValue(row, ["status", "mode", "result"]),
      detail: firstValue(row, ["summary", "strategy_id", "event_type"]) || "Persisted run context",
    })),
    ...snapshot.audit_trail.orders.map((row) => ({
      title: "Order",
      source: "Orders",
      run_id: row.run_id,
      created_at: row.created_at,
      status: firstValue(row, ["status", "side"]),
      detail: [row.symbol, row.quantity, row.order_type].filter(Boolean).join(" "),
    })),
    ...snapshot.audit_trail.approvals.map((row) => ({
      title: "Approval",
      source: "Approvals",
      run_id: row.run_id,
      created_at: row.created_at,
      status: firstValue(row, ["approved", "status", "decision"]),
      detail: firstValue(row, ["reason", "message", "operator"]),
    })),
    ...snapshot.audit_trail.system_events.map((row) => ({
      title: "System Event",
      source: firstValue(row, ["event_type", "type"]) || "System",
      run_id: row.run_id,
      created_at: row.created_at,
      status: firstValue(row, ["status", "state"]),
      detail: firstValue(row, ["message", "reason", "event_type"]),
    })),
  ];
  const needle = query.trim().toLowerCase();
  if (!needle) {
    return rows;
  }
  return rows.filter((row) => JSON.stringify(row).toLowerCase().includes(needle));
}

function buildDiagnosticContext(
  snapshot: DashboardSnapshot,
  activeTab: TabName,
  displayCurrency: string,
  period: string,
  selectedRunId: string,
  selectedStrategyId: string,
): string {
  const trust = trustSummary(snapshot);
  return [
    "Symphony Maestro diagnostic context",
    `tab: ${activeTab}`,
    `display_currency: ${displayCurrency}`,
    `analysis_period: ${period}`,
    `selected_run_id: ${selectedRunId || "n/a"}`,
    `selected_strategy_id: ${selectedStrategyId || "n/a"}`,
    `verdict: ${snapshot.system_verdict.title}`,
    `verdict_reason: ${snapshot.system_verdict.copy}`,
    `freshness: ${trust.freshness}`,
    `reconciliation: ${trust.reconciliation}`,
    `fx_status: ${trust.fxStatus}`,
    `latest_snapshot: ${trust.latestSnapshot}`,
    `config_path: ${snapshot.header.config_path}`,
    `state_path: ${snapshot.header.state_path}`,
    `audit_path: ${snapshot.header.audit_path}`,
  ].join("\n");
}

function strategyMetrics(strategy: DashboardSnapshot["virtuoso_apps"]["strategies"][number]): Metric[] {
  const enabled = String(firstValue(strategy.operation.find((row) => row.item === "Enabled") || {}, ["value"]) ?? "false");
  return [
    { label: "State", value: enabled, tone: enabled === "true" ? "success" : "warning" },
    { label: "Latest Run", value: strategy.runs[0]?.created_at || "n/a" },
    { label: "Book Value", value: strategy.summary.book_value ?? "n/a" },
    { label: "Cumulative Return", value: strategy.summary.cumulative_return ?? "n/a" },
    { label: "Drawdown", value: strategy.summary.drawdown ?? "n/a", tone: "warning" },
  ];
}

function filterByPeriod(rows: Row[], period: Period): Row[] {
  if (period === "All" || rows.length < 2) {
    return rows;
  }
  const days = period === "7D" ? 7 : period === "30D" ? 30 : 90;
  const datedRows = rows
    .map((row) => ({ row, time: dateTime(firstValue(row, ["created_at", "as_of", "timestamp", "date"])) }))
    .filter((item) => Number.isFinite(item.time));
  if (!datedRows.length) {
    return rows;
  }
  const newest = Math.max(...datedRows.map((item) => item.time));
  const cutoff = newest - days * 24 * 60 * 60 * 1000;
  const filtered = datedRows.filter((item) => item.time >= cutoff).map((item) => item.row);
  return filtered.length ? filtered : rows;
}

function chartPoints(rows: Row[], key: string) {
  const values = rows
    .slice()
    .reverse()
    .map((row) => Number(row[key]))
    .filter((value) => Number.isFinite(value));
  if (values.length < 2) {
    return [];
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  return values.map((value, index) => ({
    x: 28 + (index / Math.max(values.length - 1, 1)) * 584,
    y: 200 - ((value - min) / range) * 158,
  }));
}

function columnsFor(rows: Row[]): string[] {
  const preferred = [
    "created_at",
    "as_of",
    "currency",
    "strategy_id",
    "symbol",
    "total_value",
    "value",
    "cash",
    "return",
    "cumulative_return",
    "drawdown",
    "status",
    "passed",
  ];
  const available = Array.from(new Set(rows.flatMap((row) => Object.keys(row))));
  return [
    ...preferred.filter((column) => available.includes(column)),
    ...available.filter((column) => !preferred.includes(column)),
  ];
}

function firstValue(row: Row | undefined, keys: string[]): unknown {
  if (!row) {
    return undefined;
  }
  for (const key of keys) {
    const value = row[key];
    if (value !== undefined && value !== null && value !== "") {
      return value;
    }
  }
  return undefined;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "n/a";
  }
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  if (typeof value === "object") {
    return "summary available";
  }
  return String(value);
}

function formatReadableCell(value: unknown): string {
  if (typeof value === "object" && value !== null) {
    return "summary available";
  }
  return formatValue(value);
}

function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function toneClass(tone: unknown): string {
  const normalized = String(tone || "neutral").toLowerCase();
  if (["success", "ok", "fresh", "passed", "approved", "active", "true", "yes"].includes(normalized)) {
    return "tone-success";
  }
  if (["warning", "warn", "stale", "missing", "open", "pending"].includes(normalized)) {
    return "tone-warning";
  }
  if (["danger", "fail", "failed", "error", "halted", "rejected", "false", "no"].includes(normalized)) {
    return "tone-danger";
  }
  if (normalized === "primary") {
    return "tone-primary";
  }
  return "";
}

function toneFromValue(value: unknown): Tone {
  const normalized = String(value || "").toLowerCase();
  if (["fresh", "ok", "passed", "success", "ready"].some((item) => normalized.includes(item))) {
    return "success";
  }
  if (["stale", "missing", "open", "warn"].some((item) => normalized.includes(item))) {
    return "warning";
  }
  if (["fail", "error", "halt"].some((item) => normalized.includes(item))) {
    return "danger";
  }
  return "neutral";
}

function dateTime(value: unknown): number {
  if (!value) {
    return Number.NaN;
  }
  const time = new Date(String(value)).getTime();
  return Number.isFinite(time) ? time : Number.NaN;
}
