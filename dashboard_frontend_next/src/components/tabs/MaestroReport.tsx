import type { DashboardSnapshot, PipelineNode, WorkflowPipelineApp } from "../../types";
import type { TrustSummary } from "../../utils/trust";
import { formatValue } from "../../utils/format";
import { toneClass } from "../../utils/tone";
import { MetricGrid } from "../data-display/MetricGrid";
import { PipelineGraph } from "../data-display/PipelineGraph";
import { ReadableList } from "../data-display/ReadableList";
import { ReadableTable } from "../data-display/ReadableTable";
import { Signal } from "../data-display/Signal";
import { Panel } from "../ui/Panel";

function nodeById(app: WorkflowPipelineApp, id: string): PipelineNode | undefined {
  return app.nodes.find((node) => node.id === id);
}

function miniNodes(app: WorkflowPipelineApp): PipelineNode[] {
  return app.nodes.filter((node) => ["data", "signal", "risk", "output"].includes(node.id));
}

function accountSummary(app: WorkflowPipelineApp): string {
  if (app.account_id) {
    return String(app.account_id);
  }
  const accountNode = nodeById(app, "account");
  return formatValue(accountNode?.status || "missing");
}

export function MaestroReport({
  snapshot,
  trust,
  openApp,
}: {
  snapshot: DashboardSnapshot;
  trust: TrustSummary;
  openApp: (strategyId: string) => void;
}) {
  const pipelines = snapshot.workflow_pipelines;
  const verdict = snapshot.system_verdict;

  return (
    <section className="report-stack">
      <section className="brief-marquee-strip maestro-first">
        <article className="verdict-card operation-card">
          <span className="eyebrow">Maestro Operations</span>
          <div className={`verdict-status ${toneClass(verdict.tone)}`}>
            <strong>{verdict.title}</strong>
            <p>{verdict.copy}</p>
          </div>
          <MetricGrid metrics={verdict.status_metrics} />
        </article>

        <Panel title="Operating Posture" eyebrow="Read-only control room">
          <div className="signal-list">
            <Signal label="Mode" value={snapshot.header.mode} tone="neutral" />
            <Signal label="Read-only" value={snapshot.read_only ? "yes" : "no"} tone={snapshot.read_only ? "success" : "danger"} />
            <Signal label="Orders" value={snapshot.header.order_posture} tone="neutral" />
            <Signal label="Latest Snapshot" value={trust.latestSnapshot} tone="neutral" />
          </div>
        </Panel>
      </section>

      <Panel title="Symphony Workflow" eyebrow="Data -> State">
        <PipelineGraph nodes={pipelines.system.nodes} />
      </Panel>

      <section className="analysis-grid">
        <Panel title="Attention" eyebrow="Operator queue">
          <ReadableList
            empty="No attention items in the current snapshot."
            rows={snapshot.symphony_map.attention_items}
            titleKeys={["source", "title", "name", "status"]}
            detailKeys={["reason", "detail", "message"]}
            limit={6}
          />
        </Panel>
        <Panel title="Health / Halt / Risk" eyebrow="Checks">
          <ReadableTable rows={snapshot.operator_cockpit.health_checks} limit={8} />
        </Panel>
      </section>

      <section className="analysis-grid">
        <Panel title="Freshness" eyebrow="Operational inputs">
          <ReadableTable rows={snapshot.operator_cockpit.freshness} limit={8} />
        </Panel>
        <Panel title="Reconciliation" eyebrow="State trust">
          <ReadableTable rows={snapshot.symphony_map.verdict_reason_rows} limit={8} />
        </Panel>
      </section>

      <Panel title="Virtuoso App Pipelines" eyebrow="Configured order">
        <div className="app-pipeline-grid">
          {pipelines.apps.map((app) => {
            const signal = nodeById(app, "signal");
            const risk = nodeById(app, "risk");
            const output = nodeById(app, "output");
            return (
              <button
                key={app.strategy_id}
                className="app-pipeline-card"
                type="button"
                onClick={() => openApp(app.strategy_id)}
              >
                <span className="eyebrow">Virtuoso App</span>
                <strong>{app.display_name}</strong>
                <div className="app-pipeline-facts">
                  <span>Account {accountSummary(app)}</span>
                  <span>Signal {formatValue(signal?.status || "missing")}</span>
                  <span>Risk {formatValue(risk?.status || "n/a")}</span>
                  <span>Output {formatValue(output?.status || "n/a")}</span>
                </div>
                <PipelineGraph nodes={miniNodes(app)} compact />
              </button>
            );
          })}
        </div>
      </Panel>
    </section>
  );
}
