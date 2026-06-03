import type { DashboardSnapshot } from "../types";
import { filterByPeriod } from "../utils/data";
import { type Period, periods } from "../viewModel";
import { CompactTable, MetricRows, Panel, Segmented, StatusPill, TerminalChart } from "../components/common";

export function PortfolioTab({ period, setPeriod, snapshot }: { period: Period; setPeriod: (period: Period) => void; snapshot: DashboardSnapshot }) {
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
