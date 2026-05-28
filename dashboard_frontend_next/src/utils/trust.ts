import type { DashboardSnapshot, Metric, Row, Tone } from "../types";
import { formatValue } from "./format";
import { toneFromValue } from "./tone";
import { firstValue } from "./data";

export type TrustSummary = {
  freshness: string;
  freshnessTone: Tone;
  reconciliation: string;
  reconciliationTone: Tone;
  fxStatus: string;
  fxTone: Tone;
  latestSnapshot: string;
};

export function trustSummary(snapshot: DashboardSnapshot): TrustSummary {
  const freshnessMetric = snapshot.symphony_map.metrics.find((metric) => metric.label === "Freshness");
  const reconciliationMetric = snapshot.symphony_map.metrics.find((metric) => metric.label === "Reconciliation");
  const brokerSnapshot = snapshot.investment_console.broker_snapshot;
  const fxSnapshot = snapshot.investment_console.fx_snapshot;
  const latestSnapshot = firstValue(brokerSnapshot, ["fetched_at", "created_at", "as_of", "timestamp"]) || "n/a";
  const fxStatus = firstValue(fxSnapshot, ["status", "fx_status", "state"]) || "n/a";

  return {
    freshness: formatValue(freshnessMetric?.value || "n/a"),
    freshnessTone: freshnessMetric?.tone || "neutral",
    reconciliation: formatValue(reconciliationMetric?.value || "n/a"),
    reconciliationTone: reconciliationMetric?.tone || "neutral",
    fxStatus: formatValue(fxStatus),
    fxTone: toneFromValue(fxStatus),
    latestSnapshot: formatValue(latestSnapshot),
  };
}

export function evidenceSummaries(snapshot: DashboardSnapshot, query: string): Row[] {
  const rows: Row[] = [
    ...snapshot.audit_trail.run_index.map((row) => ({
      title: "Run",
      source: "Run Index",
      run_id: row.run_id,
      created_at: row.created_at,
      status: firstValue(row, ["status", "mode", "result"]),
      detail: firstValue(row, ["summary", "strategy_id", "event_type"]) || "Persisted run context",
    })),
    ...snapshot.audit_trail.orders.map((row) => ({
      title: "Order",
      source: "Orders",
      run_id: row.run_id,
      created_at: row.created_at,
      status: firstValue(row, ["status", "side"]),
      detail: [row.symbol, row.quantity, row.order_type].filter(Boolean).join(" "),
    })),
    ...snapshot.audit_trail.approvals.map((row) => ({
      title: "Approval",
      source: "Approvals",
      run_id: row.run_id,
      created_at: row.created_at,
      status: firstValue(row, ["approved", "status", "decision"]),
      detail: firstValue(row, ["reason", "message", "operator"]),
    })),
    ...snapshot.audit_trail.system_events.map((row) => ({
      title: "System Event",
      source: firstValue(row, ["event_type", "type"]) || "System",
      run_id: row.run_id,
      created_at: row.created_at,
      status: firstValue(row, ["status", "state"]),
      detail: firstValue(row, ["message", "reason", "event_type"]),
    })),
  ];
  const needle = query.trim().toLowerCase();
  if (!needle) {
    return rows;
  }
  return rows.filter((row) => JSON.stringify(row).toLowerCase().includes(needle));
}
