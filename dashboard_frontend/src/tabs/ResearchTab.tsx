import type { DashboardSnapshot, Row } from "../types";
import { formatPercent } from "../utils/format";
import { evidenceSummaries } from "../utils/trust";
import { appName, latestSignal, latestStrategy, signalStatusLabel, strategyMetrics } from "../viewModel";
import { CompactTable, CompareChart, MetricRows, Panel, StatusPill, seriesColor } from "../components/common";

export function ResearchTab({
  selectedStrategyId,
  setSelectedStrategyId,
  snapshot,
}: {
  selectedStrategyId: string;
  setSelectedStrategyId: (strategyId: string) => void;
  snapshot: DashboardSnapshot;
}) {
  const strategies = snapshot.virtuoso_apps.strategies;
  const selected = latestStrategy(snapshot, selectedStrategyId) || strategies[0];
  const selectedSignal = selected ? latestSignal(snapshot, selected.strategy_id) : undefined;
  const compareSeries = strategies.map((strategy, index) => {
    const rows = strategy.performance_snapshot?.series?.value?.length
      ? strategy.performance_snapshot.series.value
      : strategy.performance;
    const yKey = rows.some((row) => Number.isFinite(Number(row.current_value))) ? "current_value" : "book_value";
    return { label: appName(strategy.strategy_id), color: seriesColor(index), rows, yKey };
  });
  const appComparisonRows: Row[] = strategies.map((strategy) => {
    const signal = latestSignal(snapshot, strategy.strategy_id);
    return {
      app: appName(strategy.strategy_id),
      return: formatPercent(strategy.performance_snapshot?.latest?.cumulative_return ?? strategy.summary.cumulative_return),
      drawdown: formatPercent(strategy.summary.drawdown),
      signal: signalStatusLabel(signal, strategy),
      evidence: strategy.performance_snapshot?.quality?.status || "missing",
    };
  });
  const runs: Row[] = strategies.flatMap((strategy) =>
    strategy.runs.map((run) => ({
      app: appName(strategy.strategy_id),
      created_at: run.created_at,
      action: run.signal_action,
      symbol: run.signal_symbol,
      confidence: run.confidence,
      validation: run.validation_ok,
      run_id: run.run_id,
    })),
  );
  return (
    <section className="tab-grid research-grid">
      <Panel title="Research Control" aside={<StatusPill tone="primary">Read model</StatusPill>}>
        <label className="field">
          <span>Virtuoso App</span>
          <select value={selected?.strategy_id || ""} onChange={(event) => setSelectedStrategyId(event.target.value)}>
            {strategies.map((strategy) => (
              <option key={strategy.strategy_id} value={strategy.strategy_id}>{appName(strategy.strategy_id)}</option>
            ))}
          </select>
        </label>
        <div className="panel-subhead">App Comparison</div>
        <CompactTable
          columns={["app", "return", "drawdown", "signal"]}
          dense
          emptyLabel="No apps are configured."
          limit={6}
          rows={appComparisonRows}
        />
        <p className="muted-copy">Backtest queueing and preset writes are deferred; this tab reviews persisted strategy evidence only.</p>
      </Panel>
      <Panel className="main-chart-panel" title="Performance Compare — All Apps">
        <CompareChart series={compareSeries} title="Normalized App Performance" />
      </Panel>
      <Panel title="AI Interpretation">
        <div className="ai-copy">
          <h3>{selected ? `${appName(selected.strategy_id)} Read-model Review` : "Read-model Review"}</h3>
          <p>Compare persisted app performance, signal runs, and evidence quality here before planning any parameter change or queued backtest work.</p>
        </div>
        <MetricRows metrics={selected ? strategyMetrics(selected, selectedSignal) : []} />
      </Panel>
      <Panel className="span-2" title="Run Comparison Table">
        <CompactTable
          columns={["app", "created_at", "action", "symbol", "confidence", "validation", "run_id"]}
          emptyLabel="No signal runs are persisted."
          limit={12}
          rows={runs}
        />
      </Panel>
      <Panel title="Evidence Summaries">
        <CompactTable limit={10} rows={evidenceSummaries(snapshot, "").slice(0, 10)} />
      </Panel>
    </section>
  );
}
