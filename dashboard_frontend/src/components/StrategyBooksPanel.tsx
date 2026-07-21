import { useMemo, useState } from "react";
import type { Row, StrategyBook } from "../types";
import { Panel } from "./common";

const ROLE_ORDER = ["offensive", "defensive", "canary", "macro", "cash"];

function stateLabel(state: string | null | undefined): { text: string; cls: string } {
  if (state === "risk_on") {
    return { text: "RISK-ON", cls: "book-pill on" };
  }
  if (state === "risk_off") {
    return { text: "RISK-OFF", cls: "book-pill off" };
  }
  if (state === "cash") {
    return { text: "CASH", cls: "book-pill cash" };
  }
  return { text: "PENDING", cls: "book-pill" };
}

function gateGlyph(status: string): string {
  if (status === "pass") {
    return "✓";
  }
  if (status === "fail") {
    return "✗";
  }
  return "·";
}

function formatWeight(weight: number | null | undefined): string {
  return Number.isFinite(Number(weight)) ? `${Math.round(Number(weight) * 100)}%` : "";
}

function formatMultiple(value: number): string {
  return value >= 10 ? `${value.toFixed(1)}x` : `${value.toFixed(2)}x`;
}

function formatPct(value: unknown): string {
  const num = Number(value);
  return Number.isFinite(num) ? `${(num * 100).toFixed(1)}%` : "n/a";
}

function BookRoles({ book }: { book: StrategyBook }) {
  const signalSelected = new Set(
    Object.keys(
      Object.keys(book.signal_allocations || {}).length
        ? book.signal_allocations
        : book.allocations || {},
    ),
  );
  const roles = ROLE_ORDER.filter((role) => (book.universe[role] || []).length > 0);
  return (
    <div className="book-roles">
      {roles.map((role) => (
        <div className="book-role" key={role}>
          <em>{role}</em>
          {(book.universe[role] || []).map((symbol) => {
            const mapped = book.execution_map?.[symbol];
            const selected = signalSelected.has(symbol);
            return (
              <span className={selected ? "book-chip sel" : "book-chip"} key={symbol}>
                {symbol}
                {mapped && <i className="book-ovr">{`→${mapped}`}</i>}
              </span>
            );
          })}
        </div>
      ))}
    </div>
  );
}

function BookGates({ book, limit }: { book: StrategyBook; limit?: number }) {
  const gates = limit ? book.signal_evidence.slice(0, limit) : book.signal_evidence;
  if (!gates.length) {
    return <p className="book-empty">Gate evidence will appear after the next signal run.</p>;
  }
  return (
    <div className="book-gates">
      {gates.map((gate, index) => (
        <div className="book-gate" key={`${gate.label}-${index}`}>
          <span>{gate.label}</span>
          <b className={`gate-${gate.status}`}>
            {gate.detail} {gateGlyph(gate.status)}
          </b>
        </div>
      ))}
    </div>
  );
}

function BookPerformanceChart({ book, liveRows }: { book: StrategyBook; liveRows: Row[] }) {
  const performance = book.performance;
  const geometry = useMemo(() => {
    if (!performance || performance.equity.length < 2) {
      return null;
    }
    const W = 760;
    const H = 250;
    const PAD = { l: 44, r: 12, t: 10, b: 22 };
    const values = performance.equity;
    const dates = performance.dates;
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const y = (value: number) =>
      PAD.t + (1 - (Math.log(value) - Math.log(lo)) / (Math.log(hi) - Math.log(lo) || 1)) * (H - PAD.t - PAD.b);
    const x = (index: number) => PAD.l + (index / (values.length - 1)) * (W - PAD.l - PAD.r);
    const gridTicks = [1, 2, 4, 8, 16, 32].filter((tick) => tick >= lo && tick <= hi);
    const yearTicks: Array<[number, string]> = [];
    let lastYear = "";
    dates.forEach((dateString, index) => {
      const year = dateString.slice(0, 4);
      if (year !== lastYear && Number(year) % 3 === 1) {
        yearTicks.push([index, year]);
      }
      lastYear = year;
    });
    // Live overlay: rescale actual book TWR onto the simulated curve so both
    // series share one axis. Live rows are newest-first.
    const live = liveRows
      .slice()
      .reverse()
      .map((row) => ({
        time: String(row.created_at || ""),
        twr: Number(row.cumulative_return),
      }))
      .filter((point) => point.time && Number.isFinite(point.twr));
    let livePoints: Array<{ x: number; y: number }> = [];
    if (live.length >= 2) {
      const liveStart = live[0].time.slice(0, 10);
      let anchorIndex = dates.findIndex((dateString) => dateString >= liveStart);
      if (anchorIndex < 0) {
        anchorIndex = dates.length - 1;
      }
      const anchorValue = values[anchorIndex];
      const anchorX = x(anchorIndex);
      const lastX = W - PAD.r;
      livePoints = live.map((point, index) => ({
        x: anchorX + ((lastX - anchorX) * index) / (live.length - 1),
        y: y(anchorValue * ((1 + point.twr) / (1 + live[0].twr))),
      }));
    }
    return { W, H, PAD, values, dates, y, x, gridTicks, yearTicks, livePoints };
  }, [performance, liveRows]);

  if (!performance || !geometry) {
    return <p className="book-empty">Simulated performance cache is not available yet.</p>;
  }
  const { W, H, PAD, values, y, x, gridTicks, yearTicks, livePoints } = geometry;
  const linePoints = values
    .map((value, index) => `${x(index).toFixed(1)},${y(value).toFixed(1)}`)
    .join(" ");
  return (
    <div className="book-chart">
      <div className="book-chart-legend">
        <span className="key">
          <span className="sw simulated" /> simulated ({performance.window_start?.slice(0, 4) || ""}~
        {performance.exec_map && Object.keys(performance.exec_map).length
            ? `, exec ${Object.entries(performance.exec_map)
                .map(([from, to]) => `${from}→${to}`)
                .join(" ")}`
            : ""}
          )
        </span>
        {livePoints.length > 0 && (
          <span className="key">
            <span className="sw live" /> live (actual book TWR)
          </span>
        )}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${book.label} simulated performance`}>
        {gridTicks.map((tick) => (
          <g key={tick}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(tick)} y2={y(tick)} className="book-grid" />
            <text x={PAD.l - 6} y={y(tick) + 3} textAnchor="end" className="book-axis">
              {formatMultiple(tick)}
            </text>
          </g>
        ))}
        {yearTicks.map(([index, year]) => (
          <text key={year} x={x(index)} y={H - 6} textAnchor="middle" className="book-axis">
            {year}
          </text>
        ))}
        <polyline points={linePoints} className="book-line simulated" />
        {livePoints.length > 1 && (
          <polyline
            points={livePoints.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ")}
            className="book-line live"
          />
        )}
        {livePoints.length > 0 && (
          <circle
            cx={livePoints[livePoints.length - 1].x}
            cy={livePoints[livePoints.length - 1].y}
            r={3.5}
            className="book-dot"
          />
        )}
      </svg>
    </div>
  );
}

export function StrategyBooksPanel({
  books,
  livePerformance,
}: {
  books: StrategyBook[];
  livePerformance: Row[];
}) {
  const [expandedBookId, setExpandedBookId] = useState<string | null>(null);
  const expanded = books.find((book) => book.book_id === expandedBookId) || null;
  const latestRunAt = books[0]?.created_at || "";
  return (
    <Panel
      className="books-row"
      title="Strategy Books"
      aside={
        <span>
          {latestRunAt ? `signal run ${latestRunAt} · ` : ""}
          {books.length} books · click a card to expand
        </span>
      }
    >
      <div className="books-grid">
        {books.map((book) => {
          const state = stateLabel(book.state);
          const isExpanded = book.book_id === expandedBookId;
          return (
            <button
              className={isExpanded ? "book-card active" : "book-card"}
              key={book.book_id}
              type="button"
              onClick={() => setExpandedBookId(isExpanded ? null : book.book_id)}
            >
              <div className="book-head">
                <b>{book.label}</b>
                <small>{formatWeight(book.target_weight)}</small>
                <span className={state.cls}>{state.text}</span>
              </div>
              <BookRoles book={book} />
              <BookGates book={book} limit={4} />
              {book.rationale && <div className="book-rationale">{book.rationale}</div>}
            </button>
          );
        })}
        {expanded && (
          <div className="book-expanded">
            <div className="book-expanded-left">
              <h3>{expanded.label} · detail</h3>
              <BookGates book={expanded} />
              {expanded.performance && (
                <div className="book-gates book-backtest-metrics">
                  <div className="book-gate">
                    <span>simulated CAGR</span>
                    <b>{formatPct(expanded.performance.metrics?.cagr)}</b>
                  </div>
                  <div className="book-gate">
                    <span>simulated MDD</span>
                    <b>{formatPct(expanded.performance.metrics?.mdd)}</b>
                  </div>
                  <div className="book-gate">
                    <span>simulated Sharpe</span>
                    <b>
                      {Number.isFinite(Number(expanded.performance.metrics?.sharpe))
                        ? Number(expanded.performance.metrics?.sharpe).toFixed(2)
                        : "n/a"}
                    </b>
                  </div>
                  <div className="book-gate">
                    <span>data through</span>
                    <b>{expanded.performance.data_through || "n/a"}</b>
                  </div>
                </div>
              )}
              {expanded.performance?.note && (
                <p className="book-note">{expanded.performance.note}</p>
              )}
            </div>
            <div className="book-expanded-right">
              <BookPerformanceChart
                book={expanded}
                liveRows={livePerformance.filter(
                  (row) => String(row.book_id || "") === expanded.book_id,
                )}
              />
            </div>
          </div>
        )}
      </div>
    </Panel>
  );
}
