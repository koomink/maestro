import type { DashboardSnapshot, Row, Tone } from "../../types";
import { formatValue } from "../../utils/format";
import { strategyMetrics } from "../../utils/diagnostic";
import { MetricGrid } from "../data-display/MetricGrid";
import { LineChart } from "../data-display/LineChart";
import { ReadableTable } from "../data-display/ReadableTable";
import { Panel } from "../ui/Panel";

type StrategyAction = { busy: boolean; message: string; strategyId: string; tone?: Tone };

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
  const apps = snapshot.virtuoso_apps;
  const selected = apps.strategies.find((strategy) => strategy.strategy_id === selectedStrategyId);
  const selectedFreshness = apps.signal_freshness.strategies.find(
    (strategy) => strategy.strategy_id === selected?.strategy_id,
  );
  const selectedAction = signalAction?.strategyId === selected?.strategy_id ? signalAction : null;
  const showingOverview = selectedStrategyId === "";

  return (
    <section className="report-stack">
      <nav className="sub-tab-list" aria-label="Virtuoso apps">
        <button
          className={showingOverview ? "sub-tab active" : "sub-tab"}
          type="button"
          onClick={() => setSelectedStrategyId("")}
        >
          Overview
        </button>
        {apps.strategies.map((strategy) => (
          <button
            key={strategy.strategy_id}
            className={strategy.strategy_id === selectedStrategyId ? "sub-tab active" : "sub-tab"}
            type="button"
            onClick={() => setSelectedStrategyId(strategy.strategy_id)}
          >
            {strategy.strategy_id}
          </button>
        ))}
      </nav>

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
        </section>
      ) : !selected ? (
        <Panel title="No Virtuoso app" eyebrow="Not found">
          <p className="muted-copy">The selected strategy app was not found in the current dashboard snapshot.</p>
        </Panel>
      ) : (
        <section className="app-detail">
          <div className="dark-feature app-action-feature">
            <div className="app-action-header">
              <div>
                <span className="eyebrow">Selected App</span>
                <h2>{selected.strategy_id}</h2>
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
  );
}
