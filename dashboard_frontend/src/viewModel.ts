import type { DashboardSnapshot, Metric, Row, SignalFreshness, Tone } from "./types";
import { firstValue } from "./utils/data";
import { formatAge, formatPercent, formatValue, humanize } from "./utils/format";
import { toneFromValue } from "./utils/tone";

export const tabs = ["Portfolio", "Maestro", "Virtuoso", "Research"] as const;
export const periods = ["7D", "30D", "90D", "All"] as const;

export type TabName = (typeof tabs)[number];
export type Period = (typeof periods)[number];
export type ActionState = { busy: boolean; message: string; tone?: Tone };
export type SignalActionState = ActionState & { strategyId: string };
export type Strategy = DashboardSnapshot["virtuoso_apps"]["strategies"][number];

export function toneClass(tone?: Tone) {
  return `tone-${String(tone || "neutral").toLowerCase()}`;
}

/**
 * Non-color status glyph so tone is distinguishable without relying on color
 * alone (accessibility / color-blind users).
 */
export function toneGlyph(tone?: Tone): string {
  switch (String(tone || "neutral").toLowerCase()) {
    case "success":
      return "✓";
    case "warning":
      return "!";
    case "danger":
      return "✕";
    case "primary":
      return "◆";
    default:
      return "•";
  }
}

export function appName(strategyId: string) {
  const known: Record<string, string> = {
    tranquillo: "Tranquillo",
    crescendo_us: "Crescendo US",
    crescendo: "Crescendo",
    fugue: "Fugue",
  };
  return known[strategyId] || humanize(strategyId);
}

export function rowValue(row: Row | undefined, keys: string[]) {
  return formatValue(firstValue(row, keys));
}

export function metricValue(metrics: Metric[], label: string, fallback = "n/a") {
  return formatValue(metrics.find((metric) => metric.label === label)?.value ?? fallback);
}

export function latestRow(rows: Row[]) {
  return rows[0] || {};
}

export function latestStrategy(snapshot: DashboardSnapshot, strategyId: string) {
  return snapshot.virtuoso_apps.strategies.find((strategy) => strategy.strategy_id === strategyId);
}

export function latestSignal(snapshot: DashboardSnapshot, strategyId: string) {
  return snapshot.virtuoso_apps.signal_freshness.strategies.find(
    (row) => row.strategy_id === strategyId,
  );
}

export type SignalFreshnessRow = SignalFreshness["strategies"][number];

/**
 * Human label for a signal-freshness row: real freshness status from the
 * backend (fresh/stale/missing/failed), not just the last run's validation.
 */
export function signalStatusLabel(signal: SignalFreshnessRow | undefined, strategy: Strategy): string {
  if (signal?.status) {
    return String(signal.status);
  }
  // Fallback when the freshness card has no row for this strategy.
  const run = strategy.runs[0];
  if (run?.validation_ok === true) {
    return "fresh";
  }
  if (run?.validation_ok === false) {
    return "failed";
  }
  return "missing";
}

/** "2.1h ago · limit 24.0h" style caption for a signal-freshness row. */
export function signalAgeCaption(signal: SignalFreshnessRow | undefined): string {
  if (!signal || signal.age_seconds == null) {
    return "No signal package";
  }
  const age = `${formatAge(signal.age_seconds)} ago`;
  return signal.max_age_seconds == null ? age : `${age} · limit ${formatAge(signal.max_age_seconds)}`;
}

export function strategyMetrics(strategy: Strategy, signal?: SignalFreshnessRow): Metric[] {
  const latest = strategy.performance_snapshot?.latest || {};
  const quality = strategy.performance_snapshot?.quality?.status || "missing";
  const ret = latest.cumulative_return ?? strategy.summary.cumulative_return;
  const retNum = Number(ret);
  const signalLabel = signalStatusLabel(signal, strategy);
  return [
    { label: "Signal", value: signalLabel, tone: toneFromValue(signalLabel) },
    { label: "Current Value", value: latest.current_value ?? strategy.summary.current_value ?? strategy.summary.book_value ?? "n/a" },
    {
      label: "Return",
      value: ret == null ? "n/a" : formatPercent(ret),
      tone: !Number.isFinite(retNum) ? "neutral" : retNum > 0 ? "success" : retNum < 0 ? "danger" : "neutral",
    },
    { label: "Evidence", value: quality, tone: toneFromValue(quality) },
  ];
}
