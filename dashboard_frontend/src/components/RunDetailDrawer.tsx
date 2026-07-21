import { useEffect, useState } from "react";
import { fetchRunDetail } from "../api/snapshot";
import type { RunDetail } from "../types";
import { formatValue, humanize } from "../utils/format";
import { CompactTable, MetricRows, Panel } from "./common";

/**
 * Right-hand drawer showing the full audit context of a single run —
 * proposed orders, strategy run result, and the persisted event timeline —
 * fetched on demand from /api/dashboard/runs/{run_id}.
 */
export function RunDetailDrawer({ onClose, runId }: { onClose: () => void; runId: string }) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    setLoading(true);
    fetchRunDetail(runId)
      .then((next) => {
        if (!cancelled) {
          setDetail(next);
        }
      })
      .catch((loadError: unknown) => {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : "Run detail is unavailable");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const strategyRun = detail?.strategy_runs[0];
  const summaryEntries = detail
    ? Object.entries(detail.summary).filter(([, count]) => count > 0)
    : [];

  return (
    <aside className="console-drawer run-drawer open" aria-label="Run detail drawer">
      <header className="console-head">
        <div>
          <span className="eyebrow">Run Detail</span>
          <h2 title={runId}>{runId}</h2>
        </div>
        <button type="button" onClick={onClose} aria-label="Close run detail">✕</button>
      </header>
      {loading && <p className="muted-copy">Loading run detail…</p>}
      {error && <p className="muted-copy tone-danger">{error}</p>}
      {detail && (
        <div className="run-drawer-body">
          {summaryEntries.length > 0 && (
            <div className="run-summary-pills">
              {summaryEntries.map(([kind, count]) => (
                <span className="status-pill tone-neutral" key={kind}>
                  {humanize(kind)}: {count}
                </span>
              ))}
            </div>
          )}
          {strategyRun && (
            <Panel title="Strategy Run">
              <MetricRows
                metrics={[
                  { label: "Strategy", value: strategyRun.strategy_id },
                  { label: "Action", value: strategyRun.signal_action },
                  { label: "Symbol", value: strategyRun.signal_symbol },
                  { label: "Confidence", value: strategyRun.confidence },
                  {
                    label: "Validation",
                    value: strategyRun.validation_ok === true ? "passed" : strategyRun.validation_ok === false ? "failed" : "n/a",
                    tone: strategyRun.validation_ok === true ? "success" : strategyRun.validation_ok === false ? "danger" : "neutral",
                  },
                ]}
              />
              {typeof strategyRun.rationale === "string" && strategyRun.rationale && (
                <p className="muted-copy run-rationale">{strategyRun.rationale}</p>
              )}
              {Array.isArray(strategyRun.risk_flags) && strategyRun.risk_flags.length > 0 && (
                <p className="muted-copy tone-warning">Risk flags: {strategyRun.risk_flags.map((flag) => formatValue(flag)).join(", ")}</p>
              )}
            </Panel>
          )}
          <Panel title={`Orders (${detail.orders.length})`}>
            <CompactTable
              columns={["symbol", "side", "quantity", "price", "notional", "approval_status"]}
              dense
              emptyLabel="No orders were persisted for this run."
              limit={10}
              rows={detail.orders}
            />
          </Panel>
          <Panel title="Timeline">
            <CompactTable
              columns={["kind", "status", "symbol", "created_at"]}
              dense
              emptyLabel="No timeline events were persisted for this run."
              limit={12}
              rows={detail.timeline}
            />
          </Panel>
        </div>
      )}
    </aside>
  );
}
