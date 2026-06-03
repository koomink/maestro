import type { DashboardSnapshot } from "../types";
import { toneFromValue } from "../utils/tone";
import { trustSummary } from "../utils/trust";
import { CompactTable, MetricRows, Panel, StatusPill } from "../components/common";
import { MaestroOntology } from "../components/MaestroOntology";

export function MaestroTab({ openApp, snapshot }: { openApp: (strategyId: string) => void; snapshot: DashboardSnapshot }) {
  const trust = trustSummary(snapshot);
  return (
    <section className="tab-grid maestro-grid">
      <Panel title="Ops Left" aside={<StatusPill tone="warning">Readonly</StatusPill>}>
        <MetricRows
          metrics={[
            { label: "Mode", value: snapshot.header.mode },
            { label: "Freshness", value: trust.freshness, tone: trust.freshnessTone },
            { label: "Reconciliation", value: trust.reconciliation, tone: trust.reconciliationTone },
            { label: "Signal Routes", value: "inspect" },
            { label: "Broker Sync", value: "active", tone: "success" },
          ]}
        />
      </Panel>
      <Panel className="pipeline-panel" title="Operational Ontology Map" aside={<span>readonly: broker sync active</span>}>
        <MaestroOntology snapshot={snapshot} openApp={openApp} />
      </Panel>
      <Panel title="Gate Right" aside={<StatusPill tone="warning">Locked</StatusPill>}>
        <div className="ai-copy">
          <h3>Readonly Meaning</h3>
          <p>Strategy routes are visible for diagnosis. Broker/account sync is the only active operational path in readonly mode.</p>
        </div>
        <MetricRows
          metrics={[
            { label: "Telegram", value: "gated", tone: "warning" },
            { label: "Execution", value: snapshot.header.order_posture, tone: "warning" },
            { label: "Published State", value: snapshot.operator_home.status, tone: toneFromValue(snapshot.operator_home.status) },
          ]}
        />
      </Panel>
      <Panel className="span-2" title="Workflow State Matrix / Audit">
        <CompactTable
          rows={[
            ...snapshot.workflow_pipelines.system.nodes.map((node) => ({
              object: node.label,
              state: node.status,
              updated: node.updated_at_display || node.updated_at,
              run_id: node.run_id,
              attention: node.next_check,
            })),
            ...snapshot.operator_cockpit.health_checks.slice(0, 4),
          ]}
          limit={10}
        />
      </Panel>
    </section>
  );
}
