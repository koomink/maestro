import { useEffect, useRef, useState } from "react";
import { fetchSnapshot, generateStrategySignal, refreshDashboardState } from "./api/snapshot";
import { ConsoleDrawer } from "./components/ConsoleDrawer";
import { RelativeTime, ShellMessage } from "./components/common";
import { TopChrome } from "./components/TopChrome";
import { MaestroTab } from "./tabs/MaestroTab";
import { PortfolioTab } from "./tabs/PortfolioTab";
import { ResearchTab } from "./tabs/ResearchTab";
import { VirtuosoTab } from "./tabs/VirtuosoTab";
import type { DashboardSnapshot } from "./types";
import { buildDiagnosticContext } from "./utils/diagnostic";
import type { ActionState, Period, SignalActionState, TabName } from "./viewModel";

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

export function App() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [displayCurrency, setDisplayCurrency] = useState<"KRW" | "USD">("KRW");
  const [activeTab, setActiveTab] = useState<TabName>("Portfolio");
  const [period, setPeriod] = useState<Period>("30D");
  const [consoleOpen, setConsoleOpen] = useState(false);
  const [consoleQuery, setConsoleQuery] = useState("");
  const [selectedRunId, setSelectedRunId] = useState("");
  const [selectedStrategyId, setSelectedStrategyId] = useState("");
  const [copyState, setCopyState] = useState("Copy diagnostic context");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null);
  const [refreshAction, setRefreshAction] = useState<ActionState>({
    busy: false,
    message: "",
    tone: "neutral",
  });
  const [signalAction, setSignalAction] = useState<SignalActionState>({
    busy: false,
    message: "",
    strategyId: "",
    tone: "neutral",
  });
  const refreshMessageTimer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (refreshMessageTimer.current != null) {
        window.clearTimeout(refreshMessageTimer.current);
      }
    };
  }, []);

  // Self-healing snapshot polling: load on mount, then refresh on an interval.
  // On failure we keep the last good snapshot and retry with backoff, so a
  // backend restart or transient error never leaves the dashboard blank — it
  // recovers on its own as soon as the server is reachable again.
  useEffect(() => {
    let cancelled = false;
    let handle = 0;
    let failures = 0;
    const STEADY_MS = 30000;

    function schedule(ms: number) {
      handle = window.setTimeout(() => void tick(false), ms);
    }

    async function tick(initial: boolean) {
      if (!initial && typeof document !== "undefined" && document.hidden) {
        schedule(STEADY_MS);
        return;
      }
      try {
        const next = await fetchSnapshot(displayCurrency);
        if (cancelled) return;
        setSnapshot(next);
        setError(null);
        setLastUpdatedAt(Date.now());
        failures = 0;
      } catch (loadError) {
        if (cancelled) return;
        setError(errorMessage(loadError, "Dashboard snapshot is unavailable"));
        failures += 1;
      } finally {
        if (!cancelled && initial) setLoading(false);
      }
      if (!cancelled) {
        const backoff = Math.min(2000 * 2 ** Math.max(0, failures - 1), STEADY_MS);
        schedule(failures ? backoff : STEADY_MS);
      }
    }

    function onVisible() {
      if (typeof document !== "undefined" && !document.hidden) {
        window.clearTimeout(handle);
        void tick(false);
      }
    }

    setLoading(true);
    void tick(true);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [displayCurrency]);

  useEffect(() => {
    if (!snapshot) {
      return;
    }
    const stillExists = selectedRunId
      ? snapshot.audit_trail.run_index.some((row) => String(row.run_id) === selectedRunId)
      : false;
    if (!stillExists) {
      setSelectedRunId(String(snapshot.audit_trail.run_index[0]?.run_id || ""));
    }
  }, [selectedRunId, snapshot]);

  async function handleRefresh() {
    // A pending hide-timer from an earlier refresh must not erase this run's
    // outcome message.
    if (refreshMessageTimer.current != null) {
      window.clearTimeout(refreshMessageTimer.current);
      refreshMessageTimer.current = null;
    }
    setRefreshAction({ busy: true, message: "Updating…", tone: "primary" });
    try {
      const result = await refreshDashboardState();
      setSnapshot(await fetchSnapshot(displayCurrency));
      setError(null);
      setLastUpdatedAt(Date.now());
      const overall = result.signal_freshness?.overall || "unknown";
      setRefreshAction({
        busy: false,
        message: `Synced ${result.accounts_synced} account(s) · signals ${overall}`,
        tone: overall === "fresh" ? "success" : "warning",
      });
      refreshMessageTimer.current = window.setTimeout(() => {
        refreshMessageTimer.current = null;
        setRefreshAction((prev) => (prev.busy ? prev : { ...prev, message: "" }));
      }, 8000);
    } catch (refreshError) {
      setRefreshAction({ busy: false, message: errorMessage(refreshError, "Dashboard refresh failed"), tone: "danger" });
    }
  }

  async function handleGenerateSignal(strategyId: string) {
    setSelectedStrategyId(strategyId);
    setSignalAction({
      busy: true,
      message: `Generating proposal signal for ${strategyId}...`,
      strategyId,
      tone: "primary",
    });
    try {
      const result = await generateStrategySignal(strategyId);
      setSnapshot(await fetchSnapshot(displayCurrency));
      setSignalAction({
        busy: false,
        message: `Proposal signal ready${result.signal_run_id ? `: ${result.signal_run_id}` : ""}. ${result.orders_preview_count} preview order(s); no orders submitted.`,
        strategyId,
        tone: result.action_required ? "warning" : "success",
      });
    } catch (signalError) {
      setSignalAction({
        busy: false,
        message: errorMessage(signalError, "Signal generation failed"),
        strategyId,
        tone: "danger",
      });
    }
  }

  if (loading && !snapshot) {
    return <ShellMessage title="Maestro" copy="Loading terminal dashboard..." />;
  }

  // Only fall back to the full-screen error state when there is no data at all.
  // If we still have a (possibly stale) snapshot, keep the dashboard visible and
  // surface the error as a non-destructive banner instead.
  if (!snapshot) {
    return <ShellMessage title="Maestro" copy={error || "Dashboard snapshot is unavailable."} tone="danger" />;
  }

  const selectedStrategy = snapshot.virtuoso_apps.strategies.find((strategy) => strategy.strategy_id === selectedStrategyId);
  const diagnosticContext = buildDiagnosticContext(
    snapshot,
    activeTab,
    displayCurrency,
    period,
    selectedRunId,
    selectedStrategy?.strategy_id || "",
  );

  function openVirtuosoApp(strategyId: string) {
    setSelectedStrategyId(strategyId);
    setActiveTab("Virtuoso");
  }

  const connected = !error;

  const shellClass = ["terminal-shell", consoleOpen ? "console-is-open" : "", error ? "has-banner" : ""]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={shellClass}>
      <a className="skip-link" href="#dashboard-content">Skip to dashboard content</a>
      <TopChrome
        activeTab={activeTab}
        connected={connected}
        consoleOpen={consoleOpen}
        displayCurrency={displayCurrency}
        loading={loading}
        refreshAction={refreshAction}
        setActiveTab={setActiveTab}
        setConsoleOpen={setConsoleOpen}
        setDisplayCurrency={setDisplayCurrency}
        snapshot={snapshot}
        onRefresh={() => void handleRefresh()}
      />
      {error && (
        <div className="error-banner" role="alert">
          <span><strong>Connection issue:</strong> {error}</span>
          <span className="error-banner-note">Showing last loaded data (<RelativeTime fromMs={lastUpdatedAt} />).</span>
        </div>
      )}
      <main
        className="dashboard-main"
        id="dashboard-content"
        role="tabpanel"
        aria-labelledby={`tab-${activeTab}`}
        tabIndex={-1}
      >
        {activeTab === "Portfolio" && <PortfolioTab displayCurrency={displayCurrency} period={period} setPeriod={setPeriod} snapshot={snapshot} />}
        {activeTab === "Maestro" && <MaestroTab openApp={openVirtuosoApp} snapshot={snapshot} />}
        {activeTab === "Virtuoso" && (
          <VirtuosoTab
            generatingStrategyId={signalAction.busy ? signalAction.strategyId : ""}
            onGenerateSignal={(strategyId) => void handleGenerateSignal(strategyId)}
            selectedStrategyId={selectedStrategyId}
            setSelectedStrategyId={setSelectedStrategyId}
            signalAction={signalAction.message ? signalAction : null}
            snapshot={snapshot}
          />
        )}
        {activeTab === "Research" && <ResearchTab snapshot={snapshot} />}
      </main>
      <footer className="statusbar">
        <span aria-live="polite" className={refreshAction.tone === "danger" ? "tone-danger" : undefined}>
          {refreshAction.busy ? (
            "Updating…"
          ) : refreshAction.message ? (
            refreshAction.message
          ) : (
            <>Last updated: <RelativeTime fromMs={lastUpdatedAt} /></>
          )}
        </span>
        <span>Read-only dashboard / proposal-only signals / no trading controls</span>
      </footer>
      <ConsoleDrawer
        copyState={copyState}
        diagnosticContext={diagnosticContext}
        open={consoleOpen}
        query={consoleQuery}
        selectedRunId={selectedRunId}
        setCopyState={setCopyState}
        setOpen={setConsoleOpen}
        setQuery={setConsoleQuery}
        setSelectedRunId={setSelectedRunId}
        snapshot={snapshot}
      />
    </div>
  );
}
