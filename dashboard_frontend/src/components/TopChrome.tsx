import { type KeyboardEvent as ReactKeyboardEvent } from "react";
import type { DashboardSnapshot, Row, Tone } from "../types";
import { formatAge, formatPercent, formatValue } from "../utils/format";
import { useNow } from "../utils/hooks";
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
        <AccountsTickerCell accounts={snapshot.investment_console.broker_account_overview.accounts} />
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

/** Tone precedence when several accounts disagree: worst state wins. */
const ACCOUNT_TONE_RANK: Record<string, number> = { danger: 3, warning: 2, success: 1, neutral: 0 };

/**
 * Aggregate account freshness in one cell.
 *
 * This replaced a carousel that showed one of N accounts and advanced every
 * five seconds: two thirds of the state was always hidden, the cell moved
 * while you were reading it, and the account it happened to be showing was
 * not necessarily the one that needed attention. Per-account detail lives in
 * the Account Matrix panel.
 */
function AccountsTickerCell({ accounts }: { accounts: Row[] }) {
  if (!accounts || accounts.length === 0) {
    return <TickerCell label="Accounts" value="none configured" />;
  }
  const total = accounts.length;
  const fresh = accounts.filter((acc) => String(acc.status || "") === "fresh").length;
  // Surface the account furthest past its limit — that is the one to act on.
  const worst = accounts.reduce((acc, candidate) => {
    const rank = ACCOUNT_TONE_RANK[toneFromValue(candidate.status)] ?? 0;
    const bestRank = ACCOUNT_TONE_RANK[toneFromValue(acc.status)] ?? 0;
    if (rank !== bestRank) {
      return rank > bestRank ? candidate : acc;
    }
    return Number(candidate.age_seconds ?? -1) > Number(acc.age_seconds ?? -1) ? candidate : acc;
  }, accounts[0]);
  const tone = toneFromValue(worst.status);
  const age = worst.age_seconds == null
    ? "missing"
    : `${formatAge(Number(worst.age_seconds))} · limit ${formatAge(Number(worst.max_age_seconds))}`;
  return (
    <TickerCell
      label={`Accounts ${fresh}/${total} fresh`}
      value={formatValue(worst.status || "synced")}
      delta={age}
      tone={tone}
      title={accounts
        .map((acc) => {
          const id = formatValue(acc.account_id || "account");
          const status = formatValue(acc.status || "synced");
          const detail = acc.age_seconds == null ? "missing" : `${formatAge(Number(acc.age_seconds))} old`;
          return `${id}: ${status} · ${detail}`;
        })
        .join("\n")}
    />
  );
}

function TickerCell({ label, value, delta, tone = "neutral", primary = false, title }: { label: string; value: string; delta?: string; tone?: Tone; primary?: boolean; title?: string }) {
  const showGlyph = tone !== "neutral";
  return (
    <div className={`ticker-cell ${primary ? "ticker-primary " : ""}${toneClass(tone)}`} title={title ?? `${label}: ${value} ${delta || ""}`.trim()}>
      <b>{label}</b>
      <span>
        {showGlyph && <i className="tone-glyph" aria-hidden="true">{toneGlyph(tone)}</i>}
        {value} {delta && <small>{delta}</small>}
      </span>
    </div>
  );
}
