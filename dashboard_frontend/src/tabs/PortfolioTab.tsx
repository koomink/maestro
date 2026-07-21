import { type ReactNode, useEffect, useMemo, useState } from "react";
import type { DashboardSnapshot, Row, Tone } from "../types";
import { filterByPeriod } from "../utils/data";
import { formatCompact, formatPercent, formatValue } from "../utils/format";
import { toneFromValue } from "../utils/tone";
import {
  accountDisplayLabel,
  buildAccountValuePie,
  convertValue,
  fxRate,
  latestByKey,
  numeric,
} from "../utils/portfolioMatrix";
import { appName, type Period, periods } from "../viewModel";
import {
  CompactTable,
  DonutChart,
  MetricRows,
  Panel,
  Segmented,
  StatusPill,
  TerminalChart,
} from "../components/common";

export function PortfolioTab({
  displayCurrency,
  period,
  setPeriod,
  snapshot,
}: {
  displayCurrency: "KRW" | "USD";
  period: Period;
  setPeriod: (period: Period) => void;
  snapshot: DashboardSnapshot;
}) {
  const investment = snapshot.investment_console;
  const rows = filterByPeriod(investment.total_portfolio_performance, period);
  const yKey = rows.some((row) => Number.isFinite(Number(row.total_value))) ? "total_value" : "current_value";
  return (
    <section className="tab-grid portfolio-grid">
      <PortfolioKpiStrip performanceRows={investment.total_portfolio_performance} />
      <Panel title="Portfolio Pulse">
        <PortfolioPulse displayCurrency={displayCurrency} snapshot={snapshot} />
      </Panel>
      <Panel
        className="main-chart-panel"
        title="Portfolio Value / Return / Cash Flow"
        aside={<Segmented values={periods} value={period} onChange={setPeriod} />}
      >
        <TerminalChart title="Portfolio Value" rows={rows} yKey={yKey} />
      </Panel>
      <Panel title="AI Summary" aside={<StatusPill tone="primary">Read-only</StatusPill>}>
        <div className="ai-copy">
          <h3>Operator Brief</h3>
          <p>{snapshot.system_verdict.copy}</p>
        </div>
        <MetricRows metrics={snapshot.system_verdict.capital_summary.slice(0, 5)} />
      </Panel>
      <div className="portfolio-matrix-row">
        <Panel title="Account Matrix">
          <AccountMatrixPanel displayCurrency={displayCurrency} investment={investment} />
        </Panel>
        <Panel title="App Matrix">
          <AppMatrixPanel displayCurrency={displayCurrency} snapshot={snapshot} />
        </Panel>
        <Panel title="Holdings / Positions">
          <HoldingsTable investment={investment} />
        </Panel>
      </div>
    </section>
  );
}

/**
 * Top-line performance KPIs from the total-portfolio performance read model:
 * NAV, latest period return, cumulative return, drawdown, reconciliation.
 */
function PortfolioKpiStrip({ performanceRows }: { performanceRows: Row[] }) {
  const latest = performanceRows[0] || {};
  const periodReturn = numeric(latest.daily_return ?? latest.period_return);
  const cumulativeReturn = numeric(latest.cumulative_return);
  const drawdown = numeric(latest.drawdown);
  const cashFlow = numeric(latest.cash_flow);
  const totalValue = numeric(latest.total_value);
  const currency = String(latest.currency || latest.display_currency || "");
  const reconciliation = String(latest.reconciliation_status || "n/a");
  return (
    <div className="kpi-strip">
      <KpiCard
        label="Total NAV"
        value={totalValue == null ? "n/a" : `${formatValue(totalValue)} ${currency}`.trim()}
        caption={formatValue(latest.created_at)}
      />
      <KpiCard
        label="Period Return"
        value={periodReturn == null ? "n/a" : formatPercent(periodReturn)}
        caption="vs previous snapshot"
        tone={returnTone(periodReturn)}
      />
      <KpiCard
        label="Cumulative Return"
        value={cumulativeReturn == null ? "n/a" : formatPercent(cumulativeReturn)}
        caption="net of cash flows"
        tone={returnTone(cumulativeReturn)}
      />
      <KpiCard
        label="Drawdown"
        value={drawdown == null ? "n/a" : formatPercent(drawdown)}
        caption="from peak value"
        tone={drawdown != null && drawdown < 0 ? "danger" : "neutral"}
      />
      <KpiCard
        label="Period Cash Flow"
        value={cashFlow == null ? "n/a" : `${formatCompact(cashFlow)} ${currency}`.trim()}
        caption="deposits / withdrawals"
      />
      <KpiCard
        label="Reconciliation"
        value={reconciliation}
        caption="broker vs state"
        tone={toneFromValue(reconciliation)}
      />
    </div>
  );
}

function returnTone(value: number | null): Tone {
  if (value == null) {
    return "neutral";
  }
  return value > 0 ? "success" : value < 0 ? "danger" : "neutral";
}

function KpiCard({ caption, label, tone = "neutral", value }: { caption?: string; label: string; tone?: Tone; value: string }) {
  return (
    <div className="kpi-card">
      <b>{label}</b>
      <span className={`kpi-value tone-${tone}`}>{value}</span>
      {caption && <small>{caption}</small>}
    </div>
  );
}

function HoldingsTable({ investment }: { investment: DashboardSnapshot["investment_console"] }) {
  const positions = investment.broker_positions;
  if (!positions.length) {
    return (
      <CompactTable
        columns={["account_id", "symbol", "name", "quantity", "average_price", "current_price"]}
        dense
        limit={14}
        rows={investment.portfolio}
      />
    );
  }
  const rows: Row[] = positions
    .map((row) => {
      const quantity = numeric(row.quantity);
      const averagePrice = numeric(row.average_price);
      const currentPrice = numeric(row.current_price);
      const marketValue = numeric(row.market_value) ?? (quantity != null && currentPrice != null ? quantity * currentPrice : null);
      const pnlPct = averagePrice != null && averagePrice !== 0 && currentPrice != null ? currentPrice / averagePrice - 1 : null;
      return {
        account: accountDisplayLabel(row.account_id),
        symbol: row.symbol,
        name: row.name,
        quantity,
        price: currentPrice,
        value: marketValue,
        pnl: pnlPct == null ? "n/a" : formatPercent(pnlPct),
        weight: formatPercent(row.weight),
      };
    })
    .sort((a, b) => (numeric(b.value) ?? -1) - (numeric(a.value) ?? -1));
  return (
    <CompactTable
      columns={["account", "symbol", "quantity", "price", "value", "pnl", "weight"]}
      dense
      limit={14}
      rows={rows}
    />
  );
}

function AccountMatrixPanel({
  displayCurrency,
  investment,
}: {
  displayCurrency: "KRW" | "USD";
  investment: DashboardSnapshot["investment_console"];
}) {
  const accountRows = latestByKey(investment.account_performance, (row) => row.account_id).map((row) => ({
    account: accountDisplayLabel(row.account_id),
    currency: row.currency,
    value: row.total_value,
    cash: row.cash,
    exposure: row.positions_market_value,
    return: formatPercent(row.cumulative_return),
    drawdown: formatPercent(row.drawdown),
  }));
  const { slices: accountPie, excludedCount, omittedCount } = useMemo(
    () => buildAccountValuePie(investment.account_performance, displayCurrency, investment.fx_snapshot),
    [investment.account_performance, investment.fx_snapshot, displayCurrency],
  );
  const pieTotal = accountPie.reduce((sum, slice) => sum + slice.value, 0);
  return (
    <div className="matrix-panel-body">
      <DonutChart
        centerLabel="by account"
        centerValue={pieTotal > 0 ? `${formatCompact(pieTotal)} ${displayCurrency}` : "n/a"}
        slices={accountPie}
      />
      <PieNote excludedCount={excludedCount} omittedCount={omittedCount} unit="account" />
      <CompactTable
        columns={["account", "currency", "value", "cash", "exposure", "return", "drawdown"]}
        dense
        limit={6}
        rows={accountRows}
      />
    </div>
  );
}

function AppMatrixPanel({
  displayCurrency,
  snapshot,
}: {
  displayCurrency: "KRW" | "USD";
  snapshot: DashboardSnapshot;
}) {
  const fxSnapshot = snapshot.investment_console.fx_snapshot;
  const accountCurrencyByAccountId = useMemo(() => {
    const map = new Map<string, string>();
    for (const account of snapshot.investment_console.broker_account_overview.accounts) {
      const accountId = account.account_id;
      if (accountId != null && account.currency != null) {
        map.set(String(accountId), String(account.currency));
      }
    }
    return map;
  }, [snapshot.investment_console.broker_account_overview.accounts]);
  // Per-strategy currency: backend-resolved account currency first (covers
  // multi-account routing labels), then a direct broker-account lookup.
  const currencyByStrategyId = useMemo(() => {
    const map = new Map<string, string>();
    for (const app of snapshot.workflow_pipelines.apps) {
      const direct = app.account_id != null ? accountCurrencyByAccountId.get(String(app.account_id)) : undefined;
      const currency = app.account_currency ?? direct;
      if (currency != null) {
        map.set(app.strategy_id, String(currency));
      }
    }
    return map;
  }, [snapshot.workflow_pipelines.apps, accountCurrencyByAccountId]);

  const { slices: pieSlices, excludedCount, omittedCount } = useMemo(() => {
    let excluded = 0;
    let omitted = 0;
    const slices: { label: string; value: number }[] = [];
    for (const strategy of snapshot.virtuoso_apps.strategies) {
      const bookValue = numeric(strategy.summary.book_value);
      if (bookValue == null || bookValue <= 0) {
        omitted += 1;
        continue;
      }
      const currency = currencyByStrategyId.get(strategy.strategy_id);
      const converted = convertValue(bookValue, currency, displayCurrency, fxSnapshot);
      if (converted == null) {
        excluded += 1;
        continue;
      }
      slices.push({ label: appName(strategy.strategy_id), value: converted });
    }
    return { slices, excludedCount: excluded, omittedCount: omitted };
  }, [snapshot.virtuoso_apps.strategies, currencyByStrategyId, displayCurrency, fxSnapshot]);
  const pieTotal = pieSlices.reduce((sum, slice) => sum + slice.value, 0);
  const tableRows: Row[] = snapshot.virtuoso_apps.strategies.map((strategy) => ({
    strategy: appName(strategy.strategy_id),
    currency: currencyByStrategyId.get(strategy.strategy_id) ?? "n/a",
    value: strategy.summary.book_value,
    return: formatPercent(strategy.summary.cumulative_return),
    period: formatPercent(strategy.summary.period_return),
    drawdown: formatPercent(strategy.summary.drawdown),
  }));
  return (
    <div className="matrix-panel-body">
      <DonutChart
        centerLabel="by app"
        centerValue={pieTotal > 0 ? `${formatCompact(pieTotal)} ${displayCurrency}` : "n/a"}
        slices={pieSlices}
      />
      <PieNote excludedCount={excludedCount} omittedCount={omittedCount} unit="app" />
      <p className="muted-copy">
        App values are actual attributed holdings (Maestro-purchased positions only); manually
        held assets are not included.
      </p>
      <CompactTable
        columns={["strategy", "currency", "value", "return", "period", "drawdown"]}
        dense
        limit={6}
        rows={tableRows}
      />
    </div>
  );
}

function PieNote({ excludedCount, omittedCount, unit }: { excludedCount: number; omittedCount: number; unit: string }) {
  if (excludedCount <= 0 && omittedCount <= 0) {
    return null;
  }
  const parts = [
    excludedCount > 0 ? `${excludedCount} ${unit}(s) excluded — currency unknown or FX unavailable.` : null,
    omittedCount > 0 ? `${omittedCount} ${unit}(s) omitted — no positive value.` : null,
  ].filter(Boolean);
  return <p className="muted-copy">{parts.join(" ")}</p>;
}

function PortfolioPulse({ displayCurrency, snapshot }: { displayCurrency: "KRW" | "USD"; snapshot: DashboardSnapshot }) {
  // Per-row toggles default to the global display currency so the two controls
  // stay consistent; the operator can still override an individual row locally.
  const [totalCurrency, setTotalCurrency] = useState<"KRW" | "USD">(displayCurrency);
  const [cashCurrency, setCashCurrency] = useState<"KRW" | "USD">(displayCurrency);
  const [exposureCurrency, setExposureCurrency] = useState<"KRW" | "USD">(displayCurrency);
  useEffect(() => {
    setTotalCurrency(displayCurrency);
    setCashCurrency(displayCurrency);
    setExposureCurrency(displayCurrency);
  }, [displayCurrency]);
  const investment = snapshot.investment_console;
  const assetRows = investment.asset_summary_rows;
  const broker = investment.broker_summary;
  const totalRow = findAssetRow(assetRows, `Total assets in ${totalCurrency}`);
  const cash = numeric(broker.cash);
  const exposure = numeric(broker.positions_market_value);
  const totalValue = numeric(broker.total_value);
  const fallbackExposure = totalValue != null && cash != null ? Math.max(0, totalValue - cash) : null;
  const totalCash = cash;
  const totalExposure = exposure ?? fallbackExposure;
  const brokerCurrency = portfolioBaseCurrency(snapshot);
  const totalCashDisplay = displayConvertedMoney(totalCash, brokerCurrency, cashCurrency, investment.fx_snapshot);
  const totalExposureDisplay = displayConvertedMoney(
    totalExposure,
    brokerCurrency,
    exposureCurrency,
    investment.fx_snapshot,
  );
  const speedometer = useMemo(() => cashExposureRatio(totalCash, totalExposure), [totalCash, totalExposure]);
  const fxRow = findAssetRow(assetRows, "FX snapshot");
  const fxStatus = String(fxRow?.fx_status || investment.fx_snapshot.status || "missing");
  const fxDisplay = displayFxSnapshot(fxRow, investment.fx_snapshot);

  return (
    <div className="portfolio-pulse">
      <div className="pulse-rows">
        <PulseRow
          label="KRW Assets"
          value={displayAssetRow(findAssetRow(assetRows, "Native KRW assets"), "KRW")}
        />
        <PulseRow
          label="USD Assets"
          value={displayAssetRow(findAssetRow(assetRows, "Native USD assets"), "USD")}
        />
        <PulseRow
          label="Total Asset"
          value={displayAssetRow(totalRow, totalCurrency)}
          control={<CurrencyToggle label="Total asset" value={totalCurrency} onChange={setTotalCurrency} />}
          tone={totalRow?.amount == null ? "warning" : "success"}
        />
        <PulseRow label="FX" value={fxDisplay} tone={fxTone(fxStatus)} />
        <PulseRow
          label="Total Cash"
          value={totalCashDisplay}
          control={<CurrencyToggle label="Total cash" value={cashCurrency} onChange={setCashCurrency} />}
          tone={totalCashDisplay === "n/a" ? "warning" : undefined}
        />
        <PulseRow
          label="Total Exposure"
          value={totalExposureDisplay}
          control={<CurrencyToggle label="Total exposure" value={exposureCurrency} onChange={setExposureCurrency} />}
          tone={totalExposureDisplay === "n/a" ? "warning" : "primary"}
        />
      </div>
      <CashExposureSpeedometer
        cashLabel={totalCashDisplay}
        exposureLabel={totalExposureDisplay}
        ratio={speedometer}
      />
    </div>
  );
}

function PulseRow({
  control,
  label,
  tone,
  value,
}: {
  control?: ReactNode;
  label: string;
  tone?: string;
  value: string;
}) {
  return (
    <div className="pulse-row">
      <span className="pulse-row-label">
        <b>{label}</b>
      </span>
      <span className={tone ? `pulse-row-value tone-${tone}` : "pulse-row-value"}>{value}</span>
      <span className="pulse-row-switch">{control}</span>
    </div>
  );
}

function CurrencyToggle({
  label,
  onChange,
  value,
}: {
  label: string;
  onChange: (value: "KRW" | "USD") => void;
  value: "KRW" | "USD";
}) {
  return (
    <span className="pulse-toggle" role="group" aria-label={`${label} display currency`}>
      {(["KRW", "USD"] as const).map((currency) => (
        <button
          aria-pressed={currency === value}
          className={currency === value ? "active" : ""}
          key={currency}
          type="button"
          onClick={() => onChange(currency)}
        >
          {currency}
        </button>
      ))}
    </span>
  );
}

function CashExposureSpeedometer({
  cashLabel,
  exposureLabel,
  ratio,
}: {
  cashLabel: string;
  exposureLabel: string;
  ratio: number | null;
}) {
  const cashPercent = ratio == null ? "n/a" : `${Math.round((1 - ratio) * 100)}%`;
  const exposurePercent = ratio == null ? "n/a" : `${Math.round(ratio * 100)}%`;
  const needle = ratio == null ? null : speedometerNeedle(ratio);

  return (
    <div className="pulse-speedometer">
      <svg viewBox="0 0 220 136" role="img" aria-label="Total cash to total exposure ratio">
        <defs>
          <linearGradient
            id="cashExposureGradient"
            x1="28"
            y1="0"
            x2="192"
            y2="0"
            gradientUnits="userSpaceOnUse"
          >
            <stop offset="0%" stopColor="var(--blue)" />
            <stop offset="48%" stopColor="var(--cyan)" />
            <stop offset="72%" stopColor="var(--amber)" />
            <stop offset="100%" stopColor="var(--red)" />
          </linearGradient>
          <filter id="speedometerGlow" x="-20%" y="-30%" width="140%" height="160%">
            <feGaussianBlur stdDeviation="2.5" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>
        <path className="speedometer-track" d="M 31 102 A 79 79 0 0 1 189 102" />
        <path className="speedometer-arc" d="M 31 102 A 79 79 0 0 1 189 102" />
        {[0, 0.25, 0.5, 0.75, 1].map((tick) => {
          const angle = (-90 + tick * 180) * (Math.PI / 180);
          const x1 = 110 + Math.sin(angle) * 69;
          const y1 = 102 - Math.cos(angle) * 69;
          const x2 = 110 + Math.sin(angle) * 77;
          const y2 = 102 - Math.cos(angle) * 77;
          return (
            <line
              className="speedometer-tick"
              key={tick}
              x1={x1}
              y1={y1}
              x2={x2}
              y2={y2}
            />
          );
        })}
        <g className={needle == null ? "speedometer-needle missing" : "speedometer-needle"}>
          {needle ? (
            <>
              <line
                className="speedometer-needle-shadow"
                x1="110"
                y1="102"
                x2={needle.end.x}
                y2={needle.end.y}
              />
              <line
                className="speedometer-needle-line"
                x1="110"
                y1="102"
                x2={needle.end.x}
                y2={needle.end.y}
              />
              <path className="speedometer-needle-tip" d={needle.tipPath} />
            </>
          ) : null}
          <circle cx="110" cy="102" r="6" />
        </g>
        <text className="speedometer-label" x="34" y="123">Cash</text>
        <text className="speedometer-label" x="166" y="123">Exposure</text>
        <text className="speedometer-value" x="110" y="120">
          {ratio == null ? "n/a" : `${Math.round(ratio * 100)}%`}
        </text>
      </svg>
      <div className="speedometer-readout">
        <span>
          <b>{cashPercent}</b> cash
        </span>
        <span>
          <b>{exposurePercent}</b> exposure
        </span>
      </div>
      <div className="speedometer-caption">
        <span>{cashLabel}</span>
        <span>{exposureLabel}</span>
      </div>
    </div>
  );
}

function findAssetRow(rows: Record<string, unknown>[], label: string) {
  return rows.find((row) => row.label === label);
}

function displayAssetRow(row: Record<string, unknown> | undefined, currency: "KRW" | "USD") {
  if (typeof row?.display === "string" && row.display !== "n/a") {
    return row.display;
  }
  return displayMoney(numeric(row?.amount), currency);
}

function displayMoney(value: number | null, currency: "KRW" | "USD") {
  return value == null ? "n/a" : `${formatValue(value)} ${currency}`;
}

function displayFxSnapshot(row: Record<string, unknown> | undefined, fxSnapshot: Record<string, unknown>) {
  const status = String(row?.fx_status || fxSnapshot.status || "missing");
  const rate = numeric(row?.amount ?? fxSnapshot.rate);
  const pair = String(row?.currency || "USD/KRW");
  if (status === "missing" || rate == null) {
    return "MISSING";
  }
  return `${rate.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${pair}`;
}

function displayConvertedMoney(
  value: number | null,
  sourceCurrency: "KRW" | "USD" | null,
  targetCurrency: "KRW" | "USD",
  fxSnapshot: Record<string, unknown>,
) {
  if (value == null || sourceCurrency == null) {
    return "n/a";
  }
  if (sourceCurrency === targetCurrency) {
    return displayMoney(value, targetCurrency);
  }
  if (String(fxSnapshot.status || "") !== "fresh") {
    return "n/a";
  }
  const rate = fxRate(sourceCurrency, targetCurrency, fxSnapshot);
  return rate == null ? "n/a" : displayMoney(value * rate, targetCurrency);
}

function portfolioBaseCurrency(snapshot: DashboardSnapshot): "KRW" | "USD" | null {
  const candidates = [
    snapshot.investment_console.broker_summary.currency,
    snapshot.investment_console.account_performance_currency,
    snapshot.investment_console.performance_snapshot.latest.currency,
    snapshot.investment_console.performance_snapshot.latest.display_currency,
    snapshot.investment_console.performance_snapshot.display_currency,
  ];
  for (const candidate of candidates) {
    const currency = String(candidate || "").toUpperCase();
    if (currency === "KRW" || currency === "USD") {
      return currency;
    }
  }
  return "KRW";
}

function cashExposureRatio(cash: number | null, exposure: number | null) {
  if (cash == null || exposure == null) {
    return null;
  }
  const total = cash + exposure;
  if (total <= 0) {
    return 0;
  }
  return Math.min(1, Math.max(0, exposure / total));
}

function speedometerNeedle(ratio: number) {
  const clamped = Math.min(1, Math.max(0, ratio));
  const angle = (-90 + clamped * 180) * (Math.PI / 180);
  const direction = { x: Math.sin(angle), y: -Math.cos(angle) };
  const normal = { x: -direction.y, y: direction.x };
  const end = {
    x: 110 + direction.x * 72,
    y: 102 + direction.y * 72,
  };
  const base = {
    x: 110 + direction.x * 58,
    y: 102 + direction.y * 58,
  };
  const left = {
    x: base.x + normal.x * 6,
    y: base.y + normal.y * 6,
  };
  const right = {
    x: base.x - normal.x * 6,
    y: base.y - normal.y * 6,
  };
  return {
    end,
    tipPath: `M ${end.x} ${end.y} L ${left.x} ${left.y} L ${right.x} ${right.y} Z`,
  };
}

function fxTone(status: string) {
  if (status === "fresh" || status === "not_needed") {
    return "success";
  }
  if (status === "stale") {
    return "danger";
  }
  if (status === "missing") {
    return "warning";
  }
  return "neutral";
}
