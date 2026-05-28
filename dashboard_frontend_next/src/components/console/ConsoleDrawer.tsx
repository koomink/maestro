import type { DashboardSnapshot } from "../../types";
import type { TrustSummary } from "../../utils/trust";
import { evidenceSummaries } from "../../utils/trust";
import { Signal } from "../data-display/Signal";
import { ReadableList } from "../data-display/ReadableList";

export function ConsoleDrawer({
  copyState,
  diagnosticContext,
  open,
  query,
  selectedRunId,
  setCopyState,
  setOpen,
  setQuery,
  setSelectedRunId,
  snapshot,
  trust,
}: {
  copyState: string;
  diagnosticContext: string;
  open: boolean;
  query: string;
  selectedRunId: string;
  setCopyState: (state: string) => void;
  setOpen: (open: boolean) => void;
  setQuery: (query: string) => void;
  setSelectedRunId: (runId: string) => void;
  snapshot: DashboardSnapshot;
  trust: TrustSummary;
}) {
  const evidenceRows = evidenceSummaries(snapshot, query);

  async function copyDiagnosticContext() {
    try {
      await navigator.clipboard.writeText(diagnosticContext);
      setCopyState("Copied");
      window.setTimeout(() => setCopyState("Copy diagnostic context"), 1600);
    } catch {
      setCopyState("Copy failed");
      window.setTimeout(() => setCopyState("Copy diagnostic context"), 1600);
    }
  }

  return (
    <aside className={open ? "console-drawer open" : "console-drawer"} aria-label="Console drawer">
      <div className="console-header">
        <div>
          <span className="eyebrow">Console</span>
          <h2>Evidence without raw logs</h2>
        </div>
        <button className="icon-button" type="button" onClick={() => setOpen(false)} aria-label="Close console">
          x
        </button>
      </div>

      <section className="console-section">
        <h3>Status</h3>
        <Signal label="Verdict" value={snapshot.system_verdict.title} tone={snapshot.system_verdict.tone} />
        <Signal label="Freshness" value={trust.freshness} tone={trust.freshnessTone} />
        <Signal label="Reconciliation" value={trust.reconciliation} tone={trust.reconciliationTone} />
        <Signal label="Latest Snapshot" value={trust.latestSnapshot} tone="neutral" />
      </section>

      <section className="console-section">
        <h3>Find Evidence</h3>
        <label className="field">
          <span>Search summaries</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="run, stale, broker, approval"
          />
        </label>
        <label className="field">
          <span>Run context</span>
          <select value={selectedRunId} onChange={(event) => setSelectedRunId(event.target.value)}>
            {snapshot.audit_trail.run_index.map((row) => (
              <option key={String(row.run_id)} value={String(row.run_id)}>
                {String(row.run_id)}
              </option>
            ))}
          </select>
        </label>
        <button className="button primary wide" type="button" onClick={() => void copyDiagnosticContext()}>
          {copyState}
        </button>
      </section>

      <section className="console-section">
        <h3>Operations Quick Check</h3>
        <ReadableList
          empty="No current operational attention."
          rows={[
            ...snapshot.operator_cockpit.attention_items,
            ...snapshot.operator_cockpit.health_checks.slice(0, 4),
            ...snapshot.operator_cockpit.halt_failure_events.slice(0, 3),
          ]}
          titleKeys={["source", "name", "check", "status", "event_type"]}
          detailKeys={["reason", "message", "detail", "created_at"]}
          limit={8}
        />
      </section>

      <section className="console-section">
        <h3>Evidence Summaries</h3>
        <ReadableList
          empty="No matching evidence summaries."
          rows={evidenceRows}
          titleKeys={["title", "source", "run_id"]}
          detailKeys={["detail", "created_at", "status"]}
          limit={10}
        />
      </section>
    </aside>
  );
}
