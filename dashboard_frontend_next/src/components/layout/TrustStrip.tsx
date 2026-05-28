import type { DashboardSnapshot } from "../../types";
import type { TrustSummary } from "../../utils/trust";
import { MiniFact } from "../ui/MiniFact";

export function TrustStrip({ snapshot, trust }: { snapshot: DashboardSnapshot; trust: TrustSummary }) {
  const fingerprint = snapshot.header.operator_config?.fingerprint || "none";
  return (
    <section className="trust-strip" aria-label="Trust strip">
      <MiniFact label="Read-only" value={snapshot.read_only ? "yes" : "no"} tone="success" />
      <MiniFact label="Mode" value={snapshot.header.mode} />
      <MiniFact label="Orders" value={snapshot.header.order_posture} tone={snapshot.header.order_posture === "armed" ? "warning" : "neutral"} />
      <MiniFact label="Currency" value={snapshot.display_currency} />
      <MiniFact label="Freshness" value={trust.freshness} tone={trust.freshnessTone} />
      <MiniFact label="Reconciliation" value={trust.reconciliation} tone={trust.reconciliationTone} />
      <MiniFact label="Config" value={fingerprint.slice(0, 12)} />
    </section>
  );
}
