import type { DashboardSnapshot } from "../types";
import { trustSummary } from "./trust";

type TabName = "Portfolio" | "Maestro" | "Virtuoso" | "Research";

export function buildDiagnosticContext(
  snapshot: DashboardSnapshot,
  activeTab: TabName,
  displayCurrency: string,
  period: string,
  selectedRunId: string,
  selectedStrategyId: string,
): string {
  const trust = trustSummary(snapshot);
  return [
    "Symphony diagnostic context",
    `tab: ${activeTab}`,
    `display_currency: ${displayCurrency}`,
    `analysis_period: ${period}`,
    `selected_run_id: ${selectedRunId || "n/a"}`,
    `selected_strategy_id: ${selectedStrategyId || "n/a"}`,
    `verdict: ${snapshot.system_verdict.title}`,
    `verdict_reason: ${snapshot.system_verdict.copy}`,
    `freshness: ${trust.freshness}`,
    `reconciliation: ${trust.reconciliation}`,
    `fx_status: ${trust.fxStatus}`,
    `latest_snapshot: ${trust.latestSnapshot}`,
    `config_path: ${snapshot.header.config_path}`,
    `state_path: ${snapshot.header.state_path}`,
    `audit_path: ${snapshot.header.audit_path}`,
  ].join("\n");
}
