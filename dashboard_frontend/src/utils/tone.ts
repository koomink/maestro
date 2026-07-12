import type { Tone } from "../types";

export function toneFromValue(value: unknown): Tone {
  const normalized = String(value || "").toLowerCase();
  if (["fresh", "ok", "passed", "success", "ready"].some((item) => normalized.includes(item))) {
    return "success";
  }
  if (["stale", "missing", "open", "warn"].some((item) => normalized.includes(item))) {
    return "warning";
  }
  if (["fail", "error", "halt"].some((item) => normalized.includes(item))) {
    return "danger";
  }
  return "neutral";
}
