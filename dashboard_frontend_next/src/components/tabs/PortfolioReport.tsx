import type { DashboardSnapshot, Row } from "../../types";
import type { TrustSummary } from "../../utils/trust";
import { filterByPeriod, firstValue } from "../../utils/data";
import { formatValue } from "../../utils/format";
import { MetricGrid } from "../data-display/MetricGrid";
import { LineChart } from "../data-display/LineChart";
import { ReadableTable } from "../data-display/ReadableTable";
import { Signal } from "../data-display/Signal";
import { KeyValueRows } from "../data-display/KeyValueRows";
import { MiniFact } from "../ui/MiniFact";
import { Panel } from "../ui/Panel";
import { SegmentedControl } from "../ui/SegmentedControl";

const periods = ["7D", "30D", "90D", "All"] as const;
type Period = (typeof periods)[number];

function brokerAccountRows(accounts: Row[]): Row[] {
  return accounts.map((account) => ({
    account_id: account.account_id,
    broker_account_id: account.broker_account_id,
    status: account.status,
    total_value: account.total_value,
    cash: account.cash,
    positions_count: account.positions_count,
    synced_at: account.created_at_display,
  }));
}

export function PortfolioReport({
  period,
  setPeriod,
  snapshot,
  trust,
}: {
  period: Period;
  setPeriod: (period: Period) => void;
  snapshot: DashboardSnapshot;
  trust: TrustSummary;
}) {
  const investment = snapshot.investment_console;
  const totalRows = investment.total_portfolio_performance;
  const latestTotal = totalRows[0] || {};
  const previousTotal = totalRows[1] || {};
  const totalPerformance = filterByPeriod(investment.total_portfolio_performance, period);
  const accountPerformance = filterByPeriod(investment.account_performance, period);
  const sleevePerformance = filterByPeriod(investment.currency_sleeve_performance, period);
  const strategyPerformance = filterByPeriod(investment.strategy_book_performance, period);
  const totalValue = firstValue(latestTotal, ["total_value", "ending_value", "market_value", "value"]);
  const totalReturn = firstValue(latestTotal, ["cumulative_return", "return", "daily_return", "pnl_pct"]);
  const dailyChange = firstValue(latestTotal, ["daily_pnl", "pnl", "change", "return_pct"]);
  const previousValue = firstValue(previousTotal, ["total_value", "ending_value", "market_value", "value"]);

  return (
    <section className="report-stack">
      <section className="brief-marquee-strip portfolio-first">
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

        <Panel title="Portfolio Trust" eyebrow="Can I trust these numbers?">
          <div className="signal-list">
            <Signal label="Reconciliation" value={trust.reconciliation} tone={trust.reconciliationTone} />
            <Signal label="Freshness" value={trust.freshness} tone={trust.freshnessTone} />
            <Signal label="FX Status" value={trust.fxStatus} tone={trust.fxTone} />
            <Signal label="Latest Snapshot" value={trust.latestSnapshot} tone="neutral" />
          </div>
        </Panel>
      </section>

      <section className="brief-grid">
        <Panel title="Capital Summary" eyebrow="Assets">
          <MetricGrid metrics={snapshot.system_verdict.capital_summary} featured />
        </Panel>
        <Panel title="Currency Sleeves" eyebrow="KRW / USD">
          <ReadableTable rows={investment.asset_summary_rows} limit={5} />
        </Panel>
      </section>

      <section className="brief-grid align-start">
        <Panel title="Broker Accounts" eyebrow="Latest snapshot">
          <div className="broker-truth-stack">
            <MetricGrid metrics={investment.broker_account_overview.metrics} />
            <ReadableTable rows={brokerAccountRows(investment.broker_account_overview.accounts)} limit={8} />
          </div>
        </Panel>
        <Panel title="Reconciliation" eyebrow="Asset confidence">
          <KeyValueRows row={investment.reconciliation} keys={["status", "passed", "created_at_display", "issue_count", "message"]} />
        </Panel>
      </section>

      <div className="report-toolbar" aria-label="Portfolio report controls">
        <SegmentedControl
          label="Period"
          value={period}
          values={[...periods]}
          onChange={(value) => setPeriod(value as Period)}
        />
      </div>

      <section className="analysis-grid">
        <LineChart title="Total Portfolio Value" rows={totalPerformance} xKey="created_at" yKey="total_value" />
        <LineChart title="Account Value" rows={accountPerformance} xKey="created_at" yKey="total_value" />
      </section>

      <section className="analysis-grid">
        <Panel title="Performance Markers" eyebrow={period}>
          <MetricGrid metrics={investment.metrics} />
        </Panel>
        <Panel title="FX & Conversion Context" eyebrow="Reporting">
          <KeyValueRows row={investment.fx_snapshot} keys={["status", "source", "rate", "as_of_display", "created_at_display"]} />
        </Panel>
      </section>

      <section className="analysis-grid wide-first">
        <Panel title="Account / App Performance" eyebrow="Attribution">
          <ReadableTable rows={investment.strategy_attribution} limit={8} />
        </Panel>
        <Panel title="Currency Sleeve Performance" eyebrow={period}>
          <ReadableTable rows={sleevePerformance} limit={8} />
        </Panel>
      </section>

      <section className="analysis-grid">
        <Panel title="TWR / MWR Context" eyebrow={period}>
          <ReadableTable rows={totalPerformance} limit={8} />
        </Panel>
        <Panel title="Cash Flow Markers" eyebrow="Strategy books">
          <ReadableTable rows={strategyPerformance} limit={8} />
        </Panel>
      </section>
    </section>
  );
}
