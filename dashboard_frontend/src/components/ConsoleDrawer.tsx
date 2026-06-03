import type { DashboardSnapshot } from "../types";
import { evidenceSummaries } from "../utils/trust";
import { CompactTable, Panel, TerminalButton } from "./common";

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
}) {
  async function copyDiagnostic() {
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
      <header className="console-head">
        <div>
          <span className="eyebrow">Console</span>
          <h2>Evidence without raw logs</h2>
        </div>
        <button type="button" onClick={() => setOpen(false)} aria-label="Close console">✕</button>
      </header>
      <label className="field">
        <span>Search summaries</span>
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="run, stale, broker, approval" />
      </label>
      <label className="field">
        <span>Run context</span>
        <select value={selectedRunId} onChange={(event) => setSelectedRunId(event.target.value)}>
          {snapshot.audit_trail.run_index.map((row) => (
            <option key={String(row.run_id)} value={String(row.run_id)}>{String(row.run_id)}</option>
          ))}
        </select>
      </label>
      <TerminalButton onClick={() => void copyDiagnostic()} variant="primary">{copyState}</TerminalButton>
      <Panel title="Evidence Summaries">
        <CompactTable rows={evidenceSummaries(snapshot, query)} limit={10} />
      </Panel>
    </aside>
  );
}
