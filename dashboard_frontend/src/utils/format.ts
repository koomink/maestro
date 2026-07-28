function formatKstTimestamp(value: string): string | null {
  if (value.includes("KST")) {
    return value;
  }
  const pattern = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/;
  const match = value.match(pattern);
  if (!match) {
    return null;
  }

  const [, year, month, day, hour, minute, second = "00", zone] = match;
  const iso = year + "-" + month + "-" + day + "T" + hour + ":" + minute + ":" + second + (zone || "Z");
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return null;
  }

  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  })
    .formatToParts(date)
    .reduce<Record<string, string>>((acc, part) => {
      if (part.type !== "literal") {
        acc[part.type] = part.value;
      }
      return acc;
    }, {});

  return parts.year + "-" + parts.month + "-" + parts.day + " " + parts.hour + ":" + parts.minute + ":" + parts.second + " KST";
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "n/a";
  }
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  if (typeof value === "object") {
    return "summary available";
  }
  const kstTimestamp = formatKstTimestamp(String(value));
  return kstTimestamp || String(value);
}

export function formatPercent(value: unknown, digits = 2): string {
  if (value === null || value === undefined || value === "") {
    return "n/a";
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return "n/a";
  }
  const pct = parsed * 100;
  return `${pct > 0 ? "+" : ""}${pct.toFixed(digits)}%`;
}

/** Compact money-style notation for tight spaces (donut centers, KPI cards). */
export function formatCompact(value: unknown): string {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return "n/a";
  }
  const abs = Math.abs(parsed);
  if (abs >= 1e12) {
    return `${(parsed / 1e12).toFixed(2)}T`;
  }
  if (abs >= 1e9) {
    return `${(parsed / 1e9).toFixed(2)}B`;
  }
  if (abs >= 1e6) {
    return `${(parsed / 1e6).toFixed(2)}M`;
  }
  if (abs >= 1e4) {
    return `${(parsed / 1e3).toFixed(1)}K`;
  }
  return parsed.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

/** Human duration from seconds ("34m", "5.2h", "1.3d"). */
export function formatAge(seconds: unknown): string {
  const parsed = Number(seconds);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return "n/a";
  }
  if (parsed < 60) {
    return `${Math.round(parsed)}s`;
  }
  // Guard on the rounded value, not the raw one, so 3599s is "1h" not "60m".
  if (Math.round(parsed / 60) < 60) {
    return `${Math.round(parsed / 60)}m`;
  }
  // Compound units past an hour: "1h 48m" reads as a duration, where "1.8h"
  // has to be converted in your head before it can be compared to a limit.
  // Rounding the minor unit can carry into the major one (7199s must be "2h",
  // not "1h 60m"), so normalise before formatting.
  if (parsed < 86400) {
    const totalMinutes = Math.round(parsed / 60);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    return minutes === 0 ? `${hours}h` : `${hours}h ${minutes}m`;
  }
  const totalHours = Math.round(parsed / 3600);
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  return hours === 0 ? `${days}d` : `${days}d ${hours}h`;
}

export function formatReadableCell(value: unknown): string {
  if (typeof value === "object" && value !== null) {
    return "summary available";
  }
  return formatValue(value);
}

const HUMANIZE_ACRONYMS = new Set(["cagr", "mdd", "fx", "pnl", "krw", "usd", "roi", "irr", "twr", "mwr"]);

export function humanize(value: string): string {
  return value
    .replaceAll("_", " ")
    .split(" ")
    .map((word) =>
      HUMANIZE_ACRONYMS.has(word.toLowerCase())
        ? word.toUpperCase()
        : word.charAt(0).toUpperCase() + word.slice(1),
    )
    .join(" ");
}
