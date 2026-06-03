import type { DashboardSnapshot, Metric, Row, Tone } from "./types";
import { firstValue } from "./utils/data";
import { formatValue, humanize } from "./utils/format";
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

export function appName(strategyId: string) {
  const known: Record<string, string> = {
    tranquillo: "Tranquillo",
    crescendo_us: "Crescendo",
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

export function strategyMetrics(strategy: Strategy): Metric[] {
  const latest = strategy.performance_snapshot?.latest || {};
  const quality = strategy.performance_snapshot?.quality?.status || "missing";
  return [
    { label: "Signal", value: latestSignalLabel(strategy), tone: toneFromValue(latestSignalLabel(strategy)) },
    { label: "Current Value", value: latest.current_value ?? strategy.summary.current_value ?? strategy.summary.book_value ?? "n/a" },
    { label: "Return", value: latest.cumulative_return ?? strategy.summary.cumulative_return ?? "n/a", tone: "success" },
    { label: "Evidence", value: quality, tone: toneFromValue(quality) },
  ];
}

export function latestSignalLabel(strategy: Strategy) {
  const run = strategy.runs[0];
  if (run?.validation_ok === true) {
    return "fresh";
  }
  if (run?.validation_ok === false) {
    return "failed";
  }
  return "missing";
}
