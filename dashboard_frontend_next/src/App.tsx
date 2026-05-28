import { useEffect, useState } from "react";
import type { DashboardSnapshot, SignalFreshness, Tone } from "./types";
import { fetchSnapshot, generateStrategySignal, loadSnapshot, refreshDashboardState } from "./api/snapshot";
import { trustSummary } from "./utils/trust";
import { buildDiagnosticContext } from "./utils/diagnostic";
import {
  TopBar,
  TrustStrip,
  DailyBrief,
  AnalysisReport,
  VirtuosoReport,
  ConsoleDrawer,
  ShellMessage,
} from "./components";

const tabs = ["Daily Brief", "Analysis Report", "Virtuoso"] as const;
const periods = ["7D", "30D", "90D", "All"] as const;

type TabName = (typeof tabs)[number];
type Period = (typeof periods)[number];
type ActionState = { busy: boolean; message: string; tone?: Tone };
type StrategyActionState = ActionState & { strategyId: string };

function statusTone(status: string | undefined): Tone {
  if (status === "fresh") {
    return "success";
  }
  if (status === "stale" || status === "missing") {
    return "warning";
  }
  if (status === "failed") {
    return "danger";
  }
  return "neutral";
}

function signalFreshnessCopy(signalFreshness: SignalFreshness): string {
  const strategyCount = signalFreshness.strategies.length;
  const suffix = strategyCount === 1 ? "1 strategy" : `${strategyCount} strategies`;
  return `Signal freshness is ${signalFreshness.overall} across ${suffix}.`;
}

function accountSyncCopy(count: number): string {
  return count === 1 ? "1 account snapshot synced" : `${count} account snapshots synced`;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function App() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [displayCurrency, setDisplayCurrency] = useState<"KRW" | "USD">("KRW");
  const [activeTab, setActiveTab] = useState<TabName>("Daily Brief");
  const [period, setPeriod] = useState<Period>("30D");
  const [consoleOpen, setConsoleOpen] = useState(false);
  const [consoleQuery, setConsoleQuery] = useState("");
  const [selectedRunId, setSelectedRunId] = useState("");
  const [selectedStrategyId, setSelectedStrategyId] = useState("");
  const [copyState, setCopyState] = useState("Copy diagnostic context");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshAction, setRefreshAction] = useState<ActionState>({
    busy: false,
    message: "Account sync + signal freshness",
    tone: "neutral",
  });
  const [signalAction, setSignalAction] = useState<StrategyActionState>({
    busy: false,
    message: "",
    strategyId: "",
    tone: "neutral",
  });

  useEffect(() => {
    void loadSnapshot(displayCurrency, setSnapshot, setLoading, setError);
  }, [displayCurrency]);

  useEffect(() => {
    if (!snapshot || selectedRunId) {
      return;
    }
    const firstRunId = String(snapshot.audit_trail.run_index[0]?.run_id || "");
    if (firstRunId) {
      setSelectedRunId(firstRunId);
    }
  }, [selectedRunId, snapshot]);

  async function handleRefresh() {
    setRefreshAction({
      busy: true,
      message: "Refreshing account snapshots and signal freshness...",
      tone: "primary",
    });
    try {
      const result = await refreshDashboardState();
      setSnapshot(await fetchSnapshot(displayCurrency));
      setRefreshAction({
        busy: false,
        message: `${accountSyncCopy(result.accounts_synced)}. ${signalFreshnessCopy(result.signal_freshness)}`,
        tone: statusTone(result.signal_freshness.overall),
      });
    } catch (refreshError) {
      setRefreshAction({
        busy: false,
        message: errorMessage(refreshError, "Dashboard refresh failed"),
        tone: "danger",
      });
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
      const previewCopy =
        result.orders_preview_count === 1 ? "1 preview order" : `${result.orders_preview_count} preview orders`;
      setSignalAction({
        busy: false,
        message: `Proposal signal ready${result.signal_run_id ? `: ${result.signal_run_id}` : ""}. ${previewCopy}; no orders submitted.`,
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
    return <ShellMessage title="Symphony Maestro" copy="Loading the editorial dashboard brief..." />;
  }

  if (error || !snapshot) {
    return (
      <ShellMessage
        title="Symphony Maestro"
        copy={error || "Dashboard snapshot is unavailable."}
        tone="danger"
      />
    );
  }

  const trust = trustSummary(snapshot);
  const selectedStrategy = snapshot.virtuoso_apps.strategies.find(
    (strategy) => strategy.strategy_id === selectedStrategyId,
  );

  const diagnosticContext = buildDiagnosticContext(
    snapshot,
    activeTab,
    displayCurrency,
    period,
    selectedRunId,
    selectedStrategy?.strategy_id || "",
  );

  return (
    <div className={consoleOpen ? "app-shell console-is-open" : "app-shell"}>
      <TopBar
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        displayCurrency={displayCurrency}
        setDisplayCurrency={setDisplayCurrency}
        loading={loading}
        refreshAction={refreshAction}
        onRefresh={() => void handleRefresh()}
        consoleOpen={consoleOpen}
        setConsoleOpen={setConsoleOpen}
      />

      <main className="page">
        <TrustStrip snapshot={snapshot} trust={trust} />
        {activeTab === "Daily Brief" && (
          <DailyBrief snapshot={snapshot} trust={trust} openConsole={() => setConsoleOpen(true)} />
        )}
        {activeTab === "Analysis Report" && (
          <AnalysisReport
            period={period}
            setPeriod={setPeriod}
            snapshot={snapshot}
          />
        )}
        {activeTab === "Virtuoso" && (
          <VirtuosoReport
            generatingStrategyId={signalAction.busy ? signalAction.strategyId : ""}
            onGenerateSignal={(strategyId) => void handleGenerateSignal(strategyId)}
            selectedStrategyId={selectedStrategyId}
            setSelectedStrategyId={setSelectedStrategyId}
            signalAction={signalAction.message ? signalAction : null}
            snapshot={snapshot}
          />
        )}
      </main>

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
        trust={trust}
      />
    </div>
  );
}
