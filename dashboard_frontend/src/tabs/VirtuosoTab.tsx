import { useEffect } from "react";
import type { DashboardSnapshot } from "../types";
import { formatValue } from "../utils/format";
import { type SignalActionState, appName, latestSignal, latestStrategy, rowValue, strategyMetrics, toneClass } from "../viewModel";
import { CompactTable, MetricRows, Panel, ShellMessage, StatusPill, SummaryPill, TerminalButton, TerminalChart } from "../components/common";

export function VirtuosoTab({
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
                    { label: "Gate", value: snapshot.header.order_posture, tone: snapshot.header.order_posture === "armed" ? "warning" : "success" },
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
