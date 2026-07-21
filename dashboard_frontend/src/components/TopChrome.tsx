import { type KeyboardEvent as ReactKeyboardEvent, useEffect, useState } from "react";
import type { DashboardSnapshot, Row, Tone } from "../types";
import { formatPercent, formatValue } from "../utils/format";
import { useNow, usePrefersReducedMotion } from "../utils/hooks";
import { toneFromValue } from "../utils/tone";
import { trustSummary } from "../utils/trust";
import { type ActionState, type TabName, latestRow, metricValue, rowValue, tabs, toneClass, toneGlyph } from "../viewModel";
import { TerminalButton } from "./common";

/** Self-ticking wall clock so the header time is actually live. */
function LiveClock() {
  const now = useNow(1000);
  const label = now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return <time className="top-clock" dateTime={now.toISOString()}>{label}</time>;
}

export function TopChrome({
  activeTab,
  connected,
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
  connected: boolean;
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
  // ARIA tablist keyboard support: Arrow/Home/End move and activate tabs.
  function onTabKeyDown(event: ReactKeyboardEvent<HTMLElement>) {
    const current = tabs.indexOf(activeTab);
    let nextIndex = current;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = (current + 1) % tabs.length;
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = (current - 1 + tabs.length) % tabs.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = tabs.length - 1;
    } else {
      return;
    }
    event.preventDefault();
    const nextTab = tabs[nextIndex];
    setActiveTab(nextTab);
    document.getElementById(`tab-${nextTab}`)?.focus();
  }

  const trust = trustSummary(snapshot);
  const configFingerprint = snapshot.header.operator_config?.fingerprint || "n/a";
  const total = latestRow(snapshot.investment_console.total_portfolio_performance);
  const navDelta = Number(total.daily_return ?? total.period_return);
  const navDeltaKnown = Number.isFinite(navDelta);
  return (
    <>
      <header className="topbar">
        <div className="brand">SYMPHONY <span>&gt;</span></div>
        <nav className="nav-tabs" role="tablist" aria-label="Dashboard sections" onKeyDown={onTabKeyDown}>
          {tabs.map((tab) => (
            <button
              aria-controls="dashboard-content"
              aria-selected={tab === activeTab}
              className={tab === activeTab ? "active" : ""}
              id={`tab-${tab}`}
              key={tab}
              role="tab"
              tabIndex={tab === activeTab ? 0 : -1}
              type="button"
              onClick={() => setActiveTab(tab)}
            >
              {tab}
            </button>
          ))}
        </nav>

        <div className="top-actions">
          <label className="visually-hidden" htmlFor="currency-select">Display currency</label>
          <select
            id="currency-select"
            aria-label="Display currency"
            value={displayCurrency}
            onChange={(event) => setDisplayCurrency(event.target.value as "KRW" | "USD")}
          >
            <option value="KRW">KRW</option>
            <option value="USD">USD</option>
          </select>
          <TerminalButton disabled={loading || refreshAction.busy} onClick={onRefresh} variant="primary">
            {refreshAction.busy ? <><i className="btn-spinner" aria-hidden="true" />Refreshing</> : "Refresh"}
          </TerminalButton>
          <TerminalButton onClick={() => setConsoleOpen(!consoleOpen)}>Console</TerminalButton>
          <LiveClock />
          <span className={connected ? "conn-status" : "conn-status conn-down"}>
            <i className={connected ? "live-dot" : "live-dot down"} aria-hidden="true" />
            {connected ? "Connected" : "Disconnected"}
          </span>
        </div>
      </header>
      <section className="ticker-strip" aria-label="Dashboard status strip">
        <TickerCell
          primary
          label="Total NAV"
          value={formatValue(total.total_value ?? total.current_value ?? total.value)}
          delta={navDeltaKnown ? formatPercent(navDelta) : displayCurrency}
          tone={navDeltaKnown ? (navDelta > 0 ? "success" : navDelta < 0 ? "danger" : "neutral") : "neutral"}
        />
        <TickerCarousel accounts={snapshot.investment_console.broker_account_overview.accounts} />
        <TickerCell label="Cash" value={metricValue(snapshot.investment_console.metrics, "Broker Cash", "n/a")} delta="ready" />
        <TickerCell label="Freshness" value={trust.freshness} delta="signals" tone={trust.freshnessTone} />
        <TickerCell label="Recon" value={trust.reconciliation} delta="state" tone={trust.reconciliationTone} />
        <TickerCell
          label="Gate"
          value={snapshot.header.order_posture}
          delta={snapshot.read_only ? "read-only" : "active"}
          tone={snapshot.header.order_posture === "armed" ? "warning" : "success"}
        />
        <TickerCell label="Config" value={String(configFingerprint).slice(0, 8)} delta="op" />
      </section>
    </>
  );
}

function TickerCarousel({ accounts }: { accounts: Row[] }) {
  const [currentIndex, setCurrentIndex] = useState(0);
  const [paused, setPaused] = useState(false);
  const reducedMotion = usePrefersReducedMotion();
  const count = accounts?.length ?? 0;

  useEffect(() => {
    if (count <= 1 || paused || reducedMotion) {
      return;
    }
    const interval = window.setInterval(() => {
      setCurrentIndex((i) => (i + 1) % count);
    }, 5000);
    return () => window.clearInterval(interval);
  }, [count, paused, reducedMotion]);

  // Keep index in range if the account list shrinks.
  useEffect(() => {
    if (currentIndex >= count && count > 0) {
      setCurrentIndex(0);
    }
  }, [count, currentIndex]);

  if (!accounts || accounts.length === 0) {
    return <TickerCell label="Account" value="synced" />;
  }

  function step(direction: 1 | -1) {
    setCurrentIndex((i) => (i + direction + count) % count);
  }

  return (
    <div
      className="ticker-cell carousel-wrapper"
      style={{ padding: 0, position: "relative" }}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
    >
      <div
        className="carousel-track"
        style={{
          transform: `translateY(-${currentIndex * 100}%)`,
          transition: reducedMotion ? "none" : "transform 0.5s cubic-bezier(0.4, 0, 0.2, 1)",
          height: "100%",
        }}
      >
        {accounts.map((acc, i) => {
          const accountId = formatValue(acc.account_id || "Account");
          const status = formatValue(acc.status || "synced");
          const timestamp = rowValue(acc, ["created_at", "as_of"]);
          const tone = toneFromValue(acc.status);
          return (
            <div
              key={String(acc.account_id ?? i)}
              className={`carousel-item ${toneClass(tone)}`}
              title={`${accountId}: ${status} ${timestamp}`.trim()}
            >
              <b>{accountId}</b>
              <span>
                <i className="tone-glyph" aria-hidden="true">{toneGlyph(tone)}</i>
                {status}
                <small>{timestamp}</small>
              </span>
            </div>
          );
        })}
      </div>
      {count > 1 && (
        <div className="carousel-controls">
          <button type="button" aria-label="Previous account" onClick={() => step(-1)}>‹</button>
          <span className="carousel-index">{currentIndex + 1}/{count}</span>
          <button type="button" aria-label="Next account" onClick={() => step(1)}>›</button>
        </div>
      )}
    </div>
  );
}

function TickerCell({ label, value, delta, tone = "neutral", primary = false }: { label: string; value: string; delta?: string; tone?: Tone; primary?: boolean }) {
  const showGlyph = tone !== "neutral";
  return (
    <div className={`ticker-cell ${primary ? "ticker-primary " : ""}${toneClass(tone)}`} title={`${label}: ${value} ${delta || ""}`.trim()}>
      <b>{label}</b>
      <span>
        {showGlyph && <i className="tone-glyph" aria-hidden="true">{toneGlyph(tone)}</i>}
        {value} {delta && <small>{delta}</small>}
      </span>
    </div>
  );
}
