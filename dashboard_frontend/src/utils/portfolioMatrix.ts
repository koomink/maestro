import type { Row } from "../types";

export function numeric(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Keeps the first row seen per key, relying on the dashboard's newest-first row contract. */
export function latestByKey(rows: Row[], keyFn: (row: Row) => unknown): Row[] {
  const seen = new Set<string>();
  const result: Row[] = [];
  for (const row of rows) {
    const key = keyFn(row);
    if (key === undefined || key === null || key === "") {
      continue;
    }
    const keyString = String(key);
    if (seen.has(keyString)) {
      continue;
    }
    seen.add(keyString);
    result.push(row);
  }
  return result;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const ACCOUNT_DISPLAY_LABELS: Record<string, string> = {
  "44667023-22": "KIS_PS",
  "44684265-01": "KIS_ISA",
  toss_brokerage: "Toss_brokerage",
};

/** Maps raw broker account ids to their operator-facing labels for display. */
export function accountDisplayLabel(accountId: unknown): string {
  const id = String(accountId ?? "Account");
  return ACCOUNT_DISPLAY_LABELS[id] ?? id;
}

export function findRate(rates: Record<string, unknown>, sourceCurrency: string, targetCurrency: string) {
  const keys = [
    `${sourceCurrency}/${targetCurrency}`,
    `${sourceCurrency}${targetCurrency}`,
    `${sourceCurrency.toLowerCase()}_${targetCurrency.toLowerCase()}`,
  ];
  for (const key of keys) {
    const rate = numeric(rates[key]);
    if (rate != null) {
      return rate;
    }
  }
  return null;
}

export function fxRate(
  sourceCurrency: "KRW" | "USD",
  targetCurrency: "KRW" | "USD",
  fxSnapshot: Record<string, unknown>,
) {
  if (sourceCurrency === targetCurrency) {
    return 1;
  }
  const rates = isRecord(fxSnapshot.rates) ? fxSnapshot.rates : {};
  const direct = findRate(rates, sourceCurrency, targetCurrency);
  if (direct != null) {
    return direct;
  }
  const inverse = findRate(rates, targetCurrency, sourceCurrency);
  if (inverse != null && inverse !== 0) {
    return 1 / inverse;
  }
  const usdKrwRate = numeric(fxSnapshot.rate);
  if (usdKrwRate == null || usdKrwRate === 0) {
    return null;
  }
  return sourceCurrency === "USD" && targetCurrency === "KRW" ? usdKrwRate : 1 / usdKrwRate;
}

export function convertValue(
  value: number | null,
  sourceCurrency: string | null | undefined,
  targetCurrency: "KRW" | "USD",
  fxSnapshot: Record<string, unknown>,
): number | null {
  if (value == null) return null;
  const source = String(sourceCurrency || "").toUpperCase();
  if (source !== "KRW" && source !== "USD") return null;
  if (source === targetCurrency) return value;
  if (String(fxSnapshot.status || "") !== "fresh") return null;
  const rate = fxRate(source, targetCurrency, fxSnapshot);
  return rate == null ? null : value * rate;
}

export type PieSlice = { label: string; value: number };

export function buildAccountValuePie(
  accountPerformanceRows: Row[],
  displayCurrency: "KRW" | "USD",
  fxSnapshot: Record<string, unknown>,
): { slices: PieSlice[]; excludedCount: number; omittedCount: number } {
  let excludedCount = 0;
  let omittedCount = 0;
  const slices: PieSlice[] = [];
  for (const row of latestByKey(accountPerformanceRows, (row) => row.account_id)) {
    const converted = convertValue(numeric(row.total_value), row.currency as string | null | undefined, displayCurrency, fxSnapshot);
    if (converted == null) {
      excludedCount += 1;
      continue;
    }
    if (converted <= 0) {
      omittedCount += 1;
      continue;
    }
    slices.push({ label: accountDisplayLabel(row.account_id), value: converted });
  }
  slices.sort((a, b) => b.value - a.value);
  return { slices, excludedCount, omittedCount };
}
