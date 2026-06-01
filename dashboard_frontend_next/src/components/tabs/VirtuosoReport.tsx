import { useState } from "react";

import type { DashboardSnapshot, Metric, Row, Tone } from "../../types";
import { formatValue } from "../../utils/format";
import { strategyMetrics } from "../../utils/diagnostic";
import { MetricGrid } from "../data-display/MetricGrid";
import { LineChart } from "../data-display/LineChart";
import { ReadableTable } from "../data-display/ReadableTable";
import { PipelineGraph } from "../data-display/PipelineGraph";
import { Panel } from "../ui/Panel";

type StrategyAction = { busy: boolean; message: string; strategyId: string; tone?: Tone };
type StrategyApp = DashboardSnapshot["virtuoso_apps"]["strategies"][number];

type AppTab = "overview" | "performance" | "backtest" | "orders" | "evidence";

const APP_TABS: Array<{ id: AppTab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "performance", label: "Performance" },
  { id: "backtest", label: "Backtest" },
  { id: "orders", label: "Orders" },
  { id: "evidence", label: "Evidence" },
];

const APP_DISPLAY_NAMES: Record<string, string> = {
  tranquillo: "Tranquillo",
  crescendo_us: "Crescendo",
  fugue: "Fugue",
};

function appDisplayName(strategyId: string): string {
  if (APP_DISPLAY_NAMES[strategyId]) {
    return APP_DISPLAY_NAMES[strategyId];
  }
  const withoutSuffix = strategyId.replace(/_(us|kr|krw|usd)$/i, "");
  return withoutSuffix
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function freshnessRows(snapshot: DashboardSnapshot): Row[] {
  const freshness = snapshot.virtuoso_apps.signal_freshness;
  return [
    { scope: "overall", status: freshness.overall },
    ...freshness.strategies.map((strategy) => ({
      strategy_id: strategy.strategy_id,
      status: strategy.status,
      latest_signal_run_id: strategy.latest_signal_run_id,
      latest_signal_at: strategy.latest_signal_at,
      age_seconds: strategy.age_seconds,
      max_age_seconds: strategy.max_age_seconds,
    })),
  ];
}

function performanceMetrics(strategy: StrategyApp): Metric[] {
  const latest = strategy.performance_snapshot?.latest || {};
  const quality = strategy.performance_snapshot?.quality?.status || "insufficient_history";
  return [
    { label: "TWR", value: latest.twr ?? "n/a", tone: quality === "ok" ? "success" : "warning" },
    { label: "Net PnL", value: latest.net_pnl ?? "n/a" },
    { label: "Cash Flow", value: latest.cumulative_cash_flow ?? "n/a" },
    { label: "Current Value", value: latest.current_value ?? latest.book_value ?? "n/a" },
    { label: "Drawdown", value: latest.drawdown ?? "n/a", tone: "warning" },
    { label: "MWR / IRR", value: latest.mwr ?? latest.irr ?? "n/a" },
  ];
}

function valueSeries(strategy: StrategyApp): Row[] {
  return strategy.performance_snapshot?.series?.value?.length
    ? strategy.performance_snapshot.series.value
    : strategy.performance;
}

function cashFlowMarkers(strategy: StrategyApp): Row[] {
  return strategy.performance_snapshot?.series?.cash_flow_markers || [];
}

function valueKey(rows: Row[]): string {
  return rows.some((row) => Number.isFinite(Number(row.current_value))) ? "current_value" : "book_value";
}

function orderRows(snapshot: DashboardSnapshot, strategyId: string): Row[] {
  return snapshot.audit_trail.orders.filter((row) => String(row.strategy_id || "") === strategyId);
}

function approvalRows(snapshot: DashboardSnapshot, strategyId: string): Row[] {
  return snapshot.audit_trail.approvals.filter((row) => String(row.strategy_id || "") === strategyId);
}

function qualityRows(strategy: StrategyApp): Row[] {
  const quality = strategy.performance_snapshot?.quality;
  if (!quality) {
    return [{ status: "insufficient_history" }];
  }
  return quality.reasons?.length ? quality.reasons : [{ status: quality.status }];
}

export function VirtuosoReport({
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
  signalAction: StrategyAction | null;
  snapshot: DashboardSnapshot;
}) {
  const [selectedAppTab, setSelectedAppTab] = useState<AppTab>("overview");
  const apps = snapshot.virtuoso_apps;
  const selected = apps.strategies.find((strategy) => strategy.strategy_id === selectedStrategyId);
  const selectedFreshness = apps.signal_freshness.strategies.find(
    (strategy) => strategy.strategy_id === selected?.strategy_id,
  );
  const selectedPipeline = snapshot.workflow_pipelines.apps.find(
    (app) => app.strategy_id === selected?.strategy_id,
  );
  const selectedAction = signalAction?.strategyId === selected?.strategy_id ? signalAction : null;
  const showingOverview = selectedStrategyId === "";

  return (
    <section className="report-stack">
      {showingOverview ? (
        <section className="report-stack">
          <MetricGrid metrics={apps.metrics} />
          <section className="analysis-grid wide-first">
            <Panel title="Virtuoso Apps" eyebrow="Overview">
              <ReadableTable rows={apps.overview} limit={10} />
            </Panel>
            <Panel title="Signal Freshness" eyebrow="Proposal status">
              <ReadableTable rows={freshnessRows(snapshot)} limit={10} />
            </Panel>
          </section>
          <section className="virtuoso-layout">
            <nav className="app-list" aria-label="Virtuoso apps">
              {apps.strategies.map((strategy) => (
                <button
                  key={strategy.strategy_id}
                  className="app-card"
                  type="button"
                  onClick={() => {
                    setSelectedStrategyId(strategy.strategy_id);
                    setSelectedAppTab("overview");
                  }}
                >
                  <span>Virtuoso App</span>
                  <strong>{appDisplayName(strategy.strategy_id)}</strong>
                  <small>{formatValue(strategy.summary.book_value ?? strategy.summary.current_value ?? "n/a")}</small>
                </button>
              ))}
            </nav>
            <Panel title="App Focus" eyebrow="Select app">
              <ReadableTable rows={apps.overview} limit={10} />
            </Panel>
          </section>
        </section>
      ) : !selected ? (
        <Panel title="No Virtuoso app" eyebrow="Not found">
          <p className="muted-copy">The selected strategy app was not found in the current dashboard snapshot.</p>
        </Panel>
      ) : (
        <section className="virtuoso-layout">
          <nav className="app-list" aria-label="Virtuoso apps">
            <button
              className="app-card"
              type="button"
              onClick={() => setSelectedStrategyId("")}
            >
              <span>All Apps</span>
              <strong>Overview</strong>
              <small>{formatValue(apps.signal_freshness.overall)}</small>
            </button>
            {apps.strategies.map((strategy) => (
              <button
                key={strategy.strategy_id}
                className={strategy.strategy_id === selectedStrategyId ? "app-card active" : "app-card"}
                type="button"
                onClick={() => {
                  setSelectedStrategyId(strategy.strategy_id);
                  setSelectedAppTab("overview");
                }}
              >
                <span>Virtuoso App</span>
                <strong>{appDisplayName(strategy.strategy_id)}</strong>
                <small>{formatValue(strategy.performance_snapshot?.quality?.status || "n/a")}</small>
              </button>
            ))}
          </nav>

          <section className="app-detail">
            <div className="dark-feature app-action-feature">
              <div className="app-action-header">
                <div>
                  <span className="eyebrow">Selected App</span>
                  <h2>{appDisplayName(selected.strategy_id)}</h2>
                </div>
                <div className="signal-action-box">
                  <button
                    className="button primary"
                    type="button"
                    onClick={() => onGenerateSignal(selected.strategy_id)}
                    disabled={Boolean(generatingStrategyId)}
                  >
                    {generatingStrategyId === selected.strategy_id ? "Generating" : "Generate Signal"}
                  </button>
                  <span>Proposal signal only. No orders or approvals are submitted.</span>
                </div>
              </div>

              <div className={`freshness-strip tone-${selectedFreshness?.status || "neutral"}`}>
                <span>Freshness</span>
                <strong>{formatValue(selectedFreshness?.status || "missing")}</strong>
                <small>{formatValue(selectedFreshness?.latest_signal_at || "No signal package")}</small>
              </div>

              {selectedAction && (
                <p className={`action-status tone-${selectedAction.tone || "neutral"}`}>{selectedAction.message}</p>
              )}

              <MetricGrid metrics={selectedAppTab === "performance" ? performanceMetrics(selected) : strategyMetrics(selected)} featured />
            </div>

            {selectedPipeline && (
              <Panel title="App Pipeline" eyebrow="Account -> Evidence">
                <PipelineGraph nodes={selectedPipeline.nodes} />
              </Panel>
            )}

            <nav className="app-subtab-list" aria-label={`${appDisplayName(selected.strategy_id)} sections`}>
              {APP_TABS.map((tab) => (
                <button
                  key={tab.id}
                  className={tab.id === selectedAppTab ? "app-subtab active" : "app-subtab"}
                  type="button"
                  onClick={() => setSelectedAppTab(tab.id)}
                >
                  {tab.label}
                </button>
              ))}
            </nav>

            {selectedAppTab === "overview" && (
              <section className="report-stack">
                <section className="analysis-grid">
                  <Panel title="Concept" eyebrow="App shape">
                    <ReadableTable rows={selected.concept} limit={6} />
                  </Panel>
                  <Panel title="Operation State" eyebrow="Current">
                    <ReadableTable rows={selected.operation} limit={6} />
                  </Panel>
                </section>
                <Panel title="Recent Runs" eyebrow="Activity">
                  <ReadableTable rows={selected.runs} limit={8} />
                </Panel>
              </section>
            )}

            {selectedAppTab === "performance" && (
              <section className="report-stack">
                <LineChart
                  title="App Value"
                  rows={valueSeries(selected)}
                  xKey="created_at"
                  yKey={valueKey(valueSeries(selected))}
                  markers={cashFlowMarkers(selected)}
                />
                <section className="analysis-grid">
                  <Panel title="Cash Flow Markers" eyebrow="Telegram attribution">
                    <ReadableTable rows={cashFlowMarkers(selected)} limit={8} />
                  </Panel>
                  <Panel title="Attribution Quality" eyebrow={formatValue(selected.performance_snapshot?.quality?.status || "n/a")}>
                    <ReadableTable rows={qualityRows(selected)} limit={8} />
                  </Panel>
                </section>
              </section>
            )}

            {selectedAppTab === "backtest" && (
              <section className="report-stack">
                <Panel title="Backtest" eyebrow="Setup">
                  <ReadableTable
                    rows={[
                      { item: "app", value: appDisplayName(selected.strategy_id) },
                      { item: "status", value: "not_configured" },
                      { item: "latest_result", value: "n/a" },
                    ]}
                    limit={6}
                  />
                </Panel>
                <LineChart
                  title="Backtest Equity Curve"
                  rows={[]}
                  xKey="created_at"
                  yKey="equity"
                />
              </section>
            )}

            {selectedAppTab === "orders" && (
              <section className="analysis-grid">
                <Panel title="Orders" eyebrow="Audit trail">
                  <ReadableTable rows={orderRows(snapshot, selected.strategy_id)} limit={10} />
                </Panel>
                <Panel title="Approvals" eyebrow="Audit trail">
                  <ReadableTable rows={approvalRows(snapshot, selected.strategy_id)} limit={10} />
                </Panel>
              </section>
            )}

            {selectedAppTab === "evidence" && (
              <section className="analysis-grid">
                <Panel title="Snapshots" eyebrow="Strategy book">
                  <ReadableTable rows={selected.snapshots} limit={10} />
                </Panel>
                <Panel title="Lineage" eyebrow="Performance">
                  <ReadableTable rows={[selected.performance_snapshot?.lineage || {}]} limit={8} />
                </Panel>
              </section>
            )}
          </section>
        </section>
      )}
    </section>
  );
}
