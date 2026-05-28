import type { DashboardSnapshot, Row } from "../../types";
import type { TrustSummary } from "../../utils/trust";
import { MiniFact } from "../ui/MiniFact";

function freshnessTooltip(rows: Row[]): string {
  if (!rows.length) {
    return "Freshness evidence is not available.";
  }

  return rows
    .map((row) => {
      const name = String(row.name || "freshness");
      const status = String(row.status || "unknown");
      const age = row.age_seconds == null ? "no age" : String(Math.round(Number(row.age_seconds))) + "s old";
      const limit = row.max_age_seconds == null ? "no limit" : "limit " + String(Math.round(Number(row.max_age_seconds))) + "s";
      return name + ": " + status + " (" + age + ", " + limit + ")";
    })
    .join("\n");
}

export function TrustStrip({ snapshot, trust }: { snapshot: DashboardSnapshot; trust: TrustSummary }) {
  const fingerprint = snapshot.header.operator_config?.fingerprint || "none";
  const freshnessDetail = freshnessTooltip(snapshot.symphony_map.freshness);
  return (
    <section className="trust-strip" aria-label="Trust strip">
      <MiniFact label="Read-only" value={snapshot.read_only ? "yes" : "no"} tone="success" />
      <MiniFact label="Mode" value={snapshot.header.mode} />
      <MiniFact label="Orders" value={snapshot.header.order_posture} tone={snapshot.header.order_posture === "armed" ? "warning" : "neutral"} />
      <MiniFact label="Currency" value={snapshot.display_currency} />
      <MiniFact label="Freshness" value={trust.freshness} tone={trust.freshnessTone} tooltip={freshnessDetail} />
      <MiniFact label="Reconciliation" value={trust.reconciliation} tone={trust.reconciliationTone} />
      <MiniFact label="Config" value={fingerprint.slice(0, 12)} />
    </section>
  );
}
