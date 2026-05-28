import type { Row } from "../types";

type Period = "7D" | "30D" | "90D" | "All";

export function filterByPeriod(rows: Row[], period: Period): Row[] {
  if (period === "All" || rows.length < 2) {
    return rows;
  }
  const days = period === "7D" ? 7 : period === "30D" ? 30 : 90;
  const datedRows = rows
    .map((row) => ({ row, time: dateTime(firstValue(row, ["created_at", "as_of", "timestamp", "date"])) }))
    .filter((item) => Number.isFinite(item.time));
  if (!datedRows.length) {
    return rows;
  }
  const newest = Math.max(...datedRows.map((item) => item.time));
  const cutoff = newest - days * 24 * 60 * 60 * 1000;
  const filtered = datedRows.filter((item) => item.time >= cutoff).map((item) => item.row);
  return filtered.length ? filtered : rows;
}

export function chartPoints(rows: Row[], key: string) {
  const values = rows
    .slice()
    .reverse()
    .map((row) => Number(row[key]))
    .filter((value) => Number.isFinite(value));
  if (values.length < 2) {
    return [];
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  return values.map((value, index) => ({
    x: 28 + (index / Math.max(values.length - 1, 1)) * 584,
    y: 200 - ((value - min) / range) * 158,
    raw: value,
  }));
}

export function columnsFor(rows: Row[]): string[] {
  const preferred = [
    "created_at",
    "as_of",
    "currency",
    "strategy_id",
    "symbol",
    "total_value",
    "value",
    "cash",
    "return",
    "cumulative_return",
    "drawdown",
    "status",
    "passed",
  ];
  const available = Array.from(new Set(rows.flatMap((row) => Object.keys(row))));
  return [
    ...preferred.filter((column) => available.includes(column)),
    ...available.filter((column) => !preferred.includes(column)),
  ];
}

export function firstValue(row: Row | undefined, keys: string[]): unknown {
  if (!row) {
    return undefined;
  }
  for (const key of keys) {
    const value = row[key];
    if (value !== undefined && value !== null && value !== "") {
      return value;
    }
  }
  return undefined;
}

export function dateTime(value: unknown): number {
  if (!value) {
    return Number.NaN;
  }
  const time = new Date(String(value)).getTime();
  return Number.isFinite(time) ? time : Number.NaN;
}
