import type { Tone } from "../../types";
import { SegmentedControl } from "../ui/SegmentedControl";

const tabs = ["Daily Brief", "Analysis Report", "Virtuoso"] as const;
type TabName = (typeof tabs)[number];

export function TopBar({
  activeTab,
  setActiveTab,
  displayCurrency,
  setDisplayCurrency,
  loading,
  refreshAction,
  onRefresh,
  consoleOpen,
  setConsoleOpen,
}: {
  activeTab: TabName;
  setActiveTab: (tab: TabName) => void;
  displayCurrency: "KRW" | "USD";
  setDisplayCurrency: (currency: "KRW" | "USD") => void;
  loading: boolean;
  refreshAction: { busy: boolean; message: string; tone?: Tone };
  onRefresh: () => void;
  consoleOpen: boolean;
  setConsoleOpen: (open: boolean) => void;
}) {
  const refreshBusy = loading || refreshAction.busy;

  return (
    <header className="topbar">
      <div className="brand-lockup">
        <span className="brand-mark" aria-hidden="true">
          *
        </span>
        <div>
          <strong>Symphony Maestro</strong>
          <span>Read-only portfolio intelligence</span>
        </div>
      </div>

      <nav className="tab-list" aria-label="Dashboard sections">
        {tabs.map((tab) => (
          <button
            key={tab}
            className={tab === activeTab ? "tab-button active" : "tab-button"}
            type="button"
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </button>
        ))}
      </nav>

      <div className="top-actions">
        <SegmentedControl
          label="Display currency"
          value={displayCurrency}
          values={["KRW", "USD"]}
          onChange={(value) => setDisplayCurrency(value as "KRW" | "USD")}
        />
        <div className={`refresh-cluster tone-${refreshAction.tone || "neutral"}`}>
          <button className="button secondary" type="button" onClick={onRefresh} disabled={refreshBusy}>
            {refreshBusy ? "Refreshing" : "Refresh"}
          </button>
          <span>{refreshAction.message}</span>
        </div>
        <button className="button primary" type="button" onClick={() => setConsoleOpen(!consoleOpen)}>
          {consoleOpen ? "Close Console" : "Open Console"}
        </button>
      </div>
    </header>
  );
}
