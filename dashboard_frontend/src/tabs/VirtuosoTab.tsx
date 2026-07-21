import { useEffect, useState } from "react";
import type { DashboardSnapshot, Row } from "../types";
import { formatCompact, formatPercent, formatValue } from "../utils/format";
import { toneFromValue } from "../utils/tone";
import {
  type SignalActionState,
  appName,
  latestSignal,
  latestStrategy,
  rowValue,
  signalAgeCaption,
  signalStatusLabel,
  strategyMetrics,
  toneClass,
  toneGlyph,
} from "../viewModel";
import { CompactTable, MetricRows, Panel, Segmented, ShellMessage, StatusPill, SummaryPill, TerminalButton, TerminalChart } from "../components/common";
import { RunDetailDrawer } from "../components/RunDetailDrawer";
import { StrategyBooksPanel } from "../components/StrategyBooksPanel";

const evidenceAreas = ["Signals", "Evidence", "Orders"] as const;
type EvidenceArea = (typeof evidenceAreas)[number];

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
  const [evidenceArea, setEvidenceArea] = useState<EvidenceArea>("Signals");
  const [openRunId, setOpenRunId] = useState<string | null>(null);
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
  const latestRunId = String(selected.runs[0]?.run_id || signal?.latest_signal_run_id || "");
  const evidenceQuality = selected.performance_snapshot?.quality;
  const strategyOrders = snapshot.audit_trail.orders.filter(
    (row) => String(row.strategy_id || "") === selected.strategy_id,
  );

  function openRun(row: Row) {
    const runId = String(row.run_id || "");
    if (runId) {
      setOpenRunId(runId);
    }
  }

  const strategyBooks = (selected.books || []).filter(
    (book) => Object.keys(book.universe || {}).length > 0,
  );
  const targetBookValue = Number(selected.summary.target_book_value);
  const metricCaptions: Record<string, string> = {
    Signal: signalAgeCaption(signal),
    "Current Value": Number.isFinite(targetBookValue)
      ? `actual holdings · target ${formatCompact(targetBookValue)}`
      : "actual attributed holdings",
    Return: "cumulative TWR, actual holdings",
    Evidence: evidenceQuality?.reasons?.length
      ? `${evidenceQuality.reasons.length} quality reason(s)`
      : "quality check passed",
  };

  return (
    <section className="tab-grid virtuoso-grid">
      <Panel title="App List" aside={<StatusPill tone="success">{strategies.length} apps</StatusPill>}>
        <div className="app-list">
          {strategies.map((strategy) => {
            const strategySignal = latestSignal(snapshot, strategy.strategy_id);
            const status = signalStatusLabel(strategySignal, strategy);
            const tone = toneFromValue(status);
            const ret = strategy.performance_snapshot?.latest?.cumulative_return ?? strategy.summary.cumulative_return;
            const retNum = ret == null ? Number.NaN : Number(ret);
            return (
              <button
                className={strategy.strategy_id === selected.strategy_id ? "app-list-item active" : "app-list-item"}
                key={strategy.strategy_id}
                type="button"
                onClick={() => setSelectedStrategyId(strategy.strategy_id)}
              >
                <b>{appName(strategy.strategy_id)}</b>
                <span>{formatValue(strategy.summary.current_value ?? strategy.summary.book_value ?? strategy.performance_snapshot?.quality?.status)}</span>
                <span className={`app-list-status ${toneClass(tone)}`}>
                  <i className="tone-glyph" aria-hidden="true">{toneGlyph(tone)}</i>
                  {status}
                  {Number.isFinite(retNum) && (
                    <em className={retNum >= 0 ? "tone-success" : "tone-danger"}>{formatPercent(ret)}</em>
                  )}
                </span>
              </button>
            );
          })}
        </div>
      </Panel>
      <Panel className="app-terminal-panel" title={`${appName(selected.strategy_id)} App Terminal`} aside={<span>selected app detail</span>}>
        <div className="app-terminal">
          <div className="app-kpis">
            {strategyMetrics(selected, signal).map((metric) => (
              <div className="app-kpi" key={metric.label}>
                <b>{metric.label}</b>
                <span className={toneClass(metric.tone)}>{formatValue(metric.value)}</span>
                <small>{metricCaptions[metric.label] || ""}</small>
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
                    { label: "Run", value: latestRunId || "n/a" },
                    { label: "Gate", value: snapshot.header.order_posture, tone: snapshot.header.order_posture === "armed" ? "warning" : "success" },
                  ]}
                />
                <TerminalButton disabled={!latestRunId} onClick={() => setOpenRunId(latestRunId)}>
                  View Run Detail
                </TerminalButton>
              </Panel>
              <Panel title="Config / Inputs">
                <MetricRows
                  metrics={[
                    ...selected.concept.slice(0, 2).map((row) => ({
                      label: formatValue(row.aspect),
                      value: row.value,
                    })),
                    ...Object.entries(selected.config || {})
                      .filter(([, value]) => typeof value !== "object" || value === null)
                      .slice(0, 4)
                      .map(([key, value]) => ({ label: key, value })),
                  ]}
                />
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
          <SummaryPill label="Freshness" value={`${formatValue(signal?.status || "missing")} · ${signalAgeCaption(signal)}`} />
          <SummaryPill label="Evidence" value={formatValue(evidenceQuality?.status || "missing")} />
          <SummaryPill label="AI Note" value="Inspect app evidence here; use Research for comparison before changing parameters." />
        </div>
      </Panel>
      {strategyBooks.length > 0 && (
        <StrategyBooksPanel books={strategyBooks} livePerformance={selected.target_performance} />
      )}
      <Panel
        className="span-2"
        title="Run Evidence"
        aside={<Segmented values={evidenceAreas} value={evidenceArea} onChange={setEvidenceArea} />}
      >
        {evidenceArea === "Signals" && (
          <CompactTable
            columns={["created_at", "run_id", "signal_action", "signal_symbol", "confidence", "validation_ok"]}
            emptyLabel="No signal runs are persisted for this app."
            limit={8}
            onRowClick={openRun}
            rows={selected.runs}
          />
        )}
        {evidenceArea === "Evidence" && (
          <CompactTable
            columns={["created_at", "run_id", "label", "book_value", "cash", "target_weight"]}
            emptyLabel="No evidence snapshots are persisted for this app."
            limit={8}
            onRowClick={openRun}
            rows={selected.snapshots}
          />
        )}
        {evidenceArea === "Orders" && (
          <CompactTable
            columns={["created_at", "run_id", "symbol", "side", "quantity", "price", "approval_status"]}
            emptyLabel="No orders are persisted for this app."
            limit={8}
            onRowClick={openRun}
            rows={strategyOrders}
          />
        )}
      </Panel>
      {openRunId && <RunDetailDrawer runId={openRunId} onClose={() => setOpenRunId(null)} />}
    </section>
  );
}
