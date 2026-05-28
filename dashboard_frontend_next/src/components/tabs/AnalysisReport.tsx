import type { DashboardSnapshot } from "../../types";
import { filterByPeriod } from "../../utils/data";
import { MetricGrid } from "../data-display/MetricGrid";
import { LineChart } from "../data-display/LineChart";
import { ReadableTable } from "../data-display/ReadableTable";
import { KeyValueRows } from "../data-display/KeyValueRows";
import { Panel } from "../ui/Panel";
import { SegmentedControl } from "../ui/SegmentedControl";
import { SectionHeader } from "../layout/SectionHeader";

const periods = ["7D", "30D", "90D", "All"] as const;
type Period = (typeof periods)[number];

export function AnalysisReport({
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
