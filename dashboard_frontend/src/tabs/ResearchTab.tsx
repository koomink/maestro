import { useState } from "react";
import type { DashboardSnapshot } from "../types";
import { firstValue } from "../utils/data";
import { evidenceSummaries } from "../utils/trust";
import { appName, latestStrategy, strategyMetrics } from "../viewModel";
import { CompactTable, MetricRows, Panel, StatusPill, TerminalButton, TerminalChart } from "../components/common";

export function ResearchTab({ snapshot }: { snapshot: DashboardSnapshot }) {
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
