import { type ReactNode, useEffect, useMemo, useState } from "react";
import type { DashboardSnapshot } from "../types";
import { filterByPeriod } from "../utils/data";
import { formatValue } from "../utils/format";
import { type Period, periods } from "../viewModel";
import {
  CompactTable,
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
      <Panel title="Portfolio Pulse">
        <PortfolioPulse displayCurrency={displayCurrency} snapshot={snapshot} />
      </Panel>
      <Panel
        className="main-chart-panel"
        title="Portfolio Value / Return / Cash Flow"
        aside={<Segmented values={periods} value={period} onChange={setPeriod} />}
      >
        <TerminalChart
          title="Portfolio Value"
          rows={rows}
          yKey={yKey}
          markers={investment.performance_snapshot.series.total_portfolio}
        />
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

function fxRate(
  sourceCurrency: "KRW" | "USD",
  targetCurrency: "KRW" | "USD",
  fxSnapshot: Record<string, unknown>,
) {
  if (sourceCurrency === targetCurrency) {
    return 1;
  }
  const rates = isRecord(fxSnapshot.rates) ? fxSnapshot.rates : {};
  const direct = findRate(rates, sourceCurrency, targetCurrency);
  if (direct != null) {
    return direct;
  }
  const inverse = findRate(rates, targetCurrency, sourceCurrency);
  if (inverse != null && inverse !== 0) {
    return 1 / inverse;
  }
  const usdKrwRate = numeric(fxSnapshot.rate);
  if (usdKrwRate == null || usdKrwRate === 0) {
    return null;
  }
  return sourceCurrency === "USD" && targetCurrency === "KRW" ? usdKrwRate : 1 / usdKrwRate;
}

function findRate(rates: Record<string, unknown>, sourceCurrency: string, targetCurrency: string) {
  const keys = [
    `${sourceCurrency}/${targetCurrency}`,
    `${sourceCurrency}${targetCurrency}`,
    `${sourceCurrency.toLowerCase()}_${targetCurrency.toLowerCase()}`,
  ];
  for (const key of keys) {
    const rate = numeric(rates[key]);
    if (rate != null) {
      return rate;
    }
  }
  return null;
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function numeric(value: unknown) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
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
