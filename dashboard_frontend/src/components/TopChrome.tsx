import { useEffect, useState } from "react";
import type { DashboardSnapshot, Tone } from "../types";
import { formatValue } from "../utils/format";
import { toneFromValue } from "../utils/tone";
import { trustSummary } from "../utils/trust";
import { type ActionState, type TabName, latestRow, metricValue, rowValue, tabs, toneClass } from "../viewModel";
import { TerminalButton } from "./common";

export function TopChrome({
  activeTab,
  consoleOpen,
  displayCurrency,
  loading,
  refreshAction,
  setActiveTab,
  setConsoleOpen,
  setDisplayCurrency,
  snapshot,
  onRefresh,
}: {
  activeTab: TabName;
  consoleOpen: boolean;
  displayCurrency: "KRW" | "USD";
  loading: boolean;
  refreshAction: ActionState;
  setActiveTab: (tab: TabName) => void;
  setConsoleOpen: (open: boolean) => void;
  setDisplayCurrency: (currency: "KRW" | "USD") => void;
  snapshot: DashboardSnapshot;
  onRefresh: () => void;
}) {
  const trust = trustSummary(snapshot);
  const configFingerprint = snapshot.header.operator_config?.fingerprint || "n/a";
  const account = snapshot.investment_console.broker_account_overview.accounts[0] || {};
  const total = latestRow(snapshot.investment_console.total_portfolio_performance);
  const researchRows = snapshot.audit_trail.strategy_run_payloads.length + snapshot.virtuoso_apps.strategies.length;
  return (
    <>
      <header className="topbar">
        <div className="brand">SYMPHONY <span>&gt;</span></div>
        <nav className="nav-tabs" aria-label="Dashboard sections">
          {tabs.map((tab) => (
            <button className={tab === activeTab ? "active" : ""} key={tab} type="button" onClick={() => setActiveTab(tab)}>
              {tab}
            </button>
          ))}
        </nav>

        <div className="top-actions">
          <select value={displayCurrency} onChange={(event) => setDisplayCurrency(event.target.value as "KRW" | "USD")}>
            <option value="KRW">KRW</option>
            <option value="USD">USD</option>
          </select>
          <TerminalButton disabled={loading || refreshAction.busy} onClick={onRefresh} variant="primary">
            {refreshAction.busy ? "Refreshing" : "Refresh"}
          </TerminalButton>
          <TerminalButton onClick={() => setConsoleOpen(!consoleOpen)}>Console</TerminalButton>
          <span>{formatValue(new Date().toISOString())}</span>
          <span><i className="live-dot" />Connected</span>
        </div>
      </header>
      <section className="ticker-strip" aria-label="Dashboard status strip">
        <TickerCell label="Total NAV" value={formatValue(total.total_value ?? total.current_value ?? total.value)} delta={displayCurrency} />
        <TickerCarousel accounts={snapshot.investment_console.broker_account_overview.accounts} />
        <TickerCell label="Cash" value={metricValue(snapshot.investment_console.metrics, "Cash", "n/a")} delta="ready" />
        <TickerCell label="Freshness" value={trust.freshness} delta="signals" tone={trust.freshnessTone} />
        <TickerCell label="Recon" value={trust.reconciliation} delta="state" tone={trust.reconciliationTone} />
        <TickerCell label="Gate" value={snapshot.header.order_posture} delta={snapshot.read_only ? "locked" : "active"} tone={snapshot.read_only ? "warning" : "success"} />
        <TickerCell label="Config" value={String(configFingerprint).slice(0, 8)} delta="op" />
        <TickerCell label="Research" value={`${researchRows} rows`} delta="read model" />
      </section>
    </>
  );
}

function TickerCarousel({ accounts }: { accounts: any[] }) {
  const [currentIndex, setCurrentIndex] = useState(0);

  useEffect(() => {
    if (!accounts || accounts.length <= 1) return;
    const interval = setInterval(() => {
      setCurrentIndex((i) => (i + 1) % accounts.length);
    }, 5000);
    return () => clearInterval(interval);
  }, [accounts]);

  if (!accounts || accounts.length === 0) {
    return <TickerCell label="Account" value="synced" />;
  }

  return (
    <div className="ticker-cell carousel-wrapper" style={{ padding: 0, position: "relative" }}>
      <div
        className="carousel-track"
        style={{
          transform: `translateY(-${currentIndex * 100}%)`,
          transition: "transform 0.5s cubic-bezier(0.4, 0, 0.2, 1)",
          height: "100%"
        }}
      >
        {accounts.map((acc: any, i: number) => {
          const accountId = formatValue(acc.account_id || "Account");
          const status = formatValue(acc.status || "synced");
          const timestamp = rowValue(acc, ["created_at", "as_of"]);
          return (
            <div
              key={acc.account_id || i}
              className={`carousel-item ${toneClass(toneFromValue(acc.status))}`}
              title={`${accountId}: ${status} ${timestamp}`.trim()}
            >
              <b>{accountId}</b>
              <span>
                {status}
                {<small>{timestamp}</small>}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function TickerCell({ label, value, delta, tone = "neutral" }: { label: string; value: string; delta?: string; tone?: Tone }) {
  return (
    <div className={`ticker-cell ${toneClass(tone)}`} title={`${label}: ${value} ${delta || ""}`.trim()}>
      <b>{label}</b>
      <span>{value} {delta && <small>{delta}</small>}</span>
    </div>
  );
}
