export function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "n/a";
  }
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  if (typeof value === "object") {
    return "summary available";
  }
  return String(value);
}

export function formatReadableCell(value: unknown): string {
  if (typeof value === "object" && value !== null) {
    return "summary available";
  }
  return formatValue(value);
}

export function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (char) => char.toUpperCase());
}
