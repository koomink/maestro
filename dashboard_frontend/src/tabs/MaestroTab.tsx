import type { DashboardSnapshot, Metric, Row } from "../types";
import { formatValue } from "../utils/format";
import { toneFromValue } from "../utils/tone";
import { trustSummary } from "../utils/trust";
import { CompactTable, MetricRows, Panel, StatusPill } from "../components/common";
import { PipelineDashboard } from "../components/PipelineDashboard";
import { toneClass, toneGlyph } from "../viewModel";

/** Operator metrics worth surfacing in the Execution Gate panel, by label. */
const GATE_METRIC_LABELS = new Set([
  "Safety State",
  "Daily Live Orders",
  "Daily Live Notional",
  "Live Order Issues",
  "Latest Live Order",
]);

export function MaestroTab({ openApp, snapshot }: { openApp: (strategyId: string) => void; snapshot: DashboardSnapshot }) {
  const trust = trustSummary(snapshot);
  const cockpit = snapshot.operator_cockpit;
  const brokerSummary = snapshot.investment_console.broker_account_overview.summary;
  const freshAccounts = Number(brokerSummary.fresh_accounts);
  const configuredAccounts = Number(brokerSummary.configured_accounts);
  const brokerSyncKnown = Number.isFinite(freshAccounts) && Number.isFinite(configuredAccounts);
  const signalFreshness = snapshot.virtuoso_apps.signal_freshness.overall;
  const gateMetrics = cockpit.metrics.filter((metric: Metric) => GATE_METRIC_LABELS.has(metric.label));
  const attentionItems = cockpit.attention_items;
  const riskRows: Row[] = cockpit.risk_decisions.map((row) => ({
    created_at: row.created_at,
    approved: row.approved,
    violations: Array.isArray(row.violations) ? row.violations.length : 0,
    account: row.account_id ?? (Array.isArray(row.account_ids) ? row.account_ids.join(", ") : "n/a"),
    run_id: row.run_id,
  }));
  const haltRows: Row[] = cockpit.halt_failure_events.map((row) => ({
    created_at: row.created_at,
    event: row.event_type,
    reason: row.reason ?? row.error_message ?? row.state,
    run_id: row.run_id,
  }));
  return (
    <section className="tab-grid maestro-grid">
      <Panel title="System Health" aside={<StatusPill tone="warning">Readonly</StatusPill>}>
        <MetricRows
          metrics={[
            { label: "Mode", value: snapshot.header.mode },
            { label: "Freshness", value: trust.freshness, tone: trust.freshnessTone },
            { label: "Reconciliation", value: trust.reconciliation, tone: trust.reconciliationTone },
            { label: "Signals", value: signalFreshness, tone: toneFromValue(signalFreshness) },
            {
              label: "Broker Sync",
              value: brokerSyncKnown ? `${freshAccounts}/${configuredAccounts} fresh` : "n/a",
              tone: brokerSyncKnown && freshAccounts === configuredAccounts ? "success" : "warning",
            },
          ]}
        />
        <div className="panel-subhead">Health Checks</div>
        <CompactTable
          columns={["check", "status"]}
          dense
          emptyLabel="No health checks were reported."
          limit={6}
          rows={cockpit.health_checks}
        />
      </Panel>
      <Panel
        className="pipeline-panel"
        title="Pipeline Dashboard"
        aside={
          <span className="pd-live">
            <i aria-hidden="true" className="pd-live-dot" />
            live · auto-refresh
          </span>
        }
      >
        <PipelineDashboard snapshot={snapshot} openApp={openApp} />
      </Panel>
      <Panel title="Execution Gate" aside={<StatusPill tone="warning">Locked</StatusPill>}>
        <MetricRows
          metrics={[
            {
              label: "Execution",
              value: snapshot.header.order_posture,
              tone: snapshot.header.order_posture === "armed" ? "warning" : "success",
            },
            { label: "Published State", value: snapshot.operator_home.status, tone: toneFromValue(snapshot.operator_home.status) },
            ...gateMetrics,
          ]}
        />
        {attentionItems.length > 0 && (
          <>
            <div className="panel-subhead">Attention</div>
            <ul className="attention-list">
              {attentionItems.slice(0, 4).map((item, index) => {
                const severity = String(item.severity || "warn");
                const tone = severity === "fail" ? "danger" : "warning";
                return (
                  <li className={toneClass(tone)} key={index}>
                    <i className="tone-glyph" aria-hidden="true">{toneGlyph(tone)}</i>
                    {formatValue(item.message)}
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </Panel>
      <Panel title="Workflow State">
        <CompactTable
          columns={["object", "state", "updated", "run_id"]}
          dense
          emptyLabel="No workflow nodes were reported."
          limit={8}
          rows={snapshot.workflow_pipelines.system.nodes.map((node) => ({
            object: node.label,
            state: node.status,
            updated: node.updated_at_display || node.updated_at,
            run_id: node.run_id,
          }))}
        />
      </Panel>
      <Panel title="Risk Decisions">
        <CompactTable
          columns={["created_at", "approved", "violations", "account"]}
          dense
          emptyLabel="No risk decisions were persisted."
          limit={6}
          rows={riskRows}
        />
      </Panel>
      <Panel title="Halt / Failure Events">
        <CompactTable
          columns={["created_at", "event", "reason"]}
          dense
          emptyLabel="No halt or failure events — system is running clean."
          limit={6}
          rows={haltRows}
        />
      </Panel>
    </section>
  );
}
