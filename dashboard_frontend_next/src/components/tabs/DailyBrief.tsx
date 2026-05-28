import type { DashboardSnapshot, Row } from "../../types";
import type { TrustSummary } from "../../utils/trust";
import { firstValue } from "../../utils/data";
import { formatValue } from "../../utils/format";
import { toneClass } from "../../utils/tone";
import { MetricGrid } from "../data-display/MetricGrid";
import { ReadableTable } from "../data-display/ReadableTable";
import { ReadableList } from "../data-display/ReadableList";
import { Signal } from "../data-display/Signal";
import { KeyValueRows } from "../data-display/KeyValueRows";
import { MiniFact } from "../ui/MiniFact";
import { Panel } from "../ui/Panel";

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

export function DailyBrief({
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
      {/* Portfolio marquee strip — replaces the old hero section */}
      <section className="brief-marquee-strip">
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

        <article className="verdict-card">
          <span className="eyebrow">System Verdict</span>
          <div className={`verdict-status ${toneClass(snapshot.system_verdict.tone)}`}>
            <strong>{snapshot.system_verdict.title}</strong>
            <p>{snapshot.system_verdict.copy}</p>
          </div>
          <MetricGrid metrics={snapshot.system_verdict.status_metrics} />
          <button className="button primary" type="button" onClick={openConsole}>
            Investigate in Console
          </button>
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
        <Panel title="Broker Truth" eyebrow="Accounts">
          <div className="broker-truth-stack">
            <MetricGrid metrics={investment.broker_account_overview.metrics} />
            <ReadableTable rows={brokerAccountRows(investment.broker_account_overview.accounts)} limit={8} />
          </div>
        </Panel>
        <Panel title="Reconciliation" eyebrow="State">
          <KeyValueRows row={investment.reconciliation} keys={["status", "passed", "created_at", "issue_count", "message"]} />
        </Panel>
      </section>
    </section>
  );
}
