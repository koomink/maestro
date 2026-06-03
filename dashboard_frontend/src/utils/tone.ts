import type { Tone } from "../types";

export function toneClass(tone: unknown): string {
  const normalized = String(tone || "neutral").toLowerCase();
  if (["success", "ok", "fresh", "passed", "approved", "active", "true", "yes"].includes(normalized)) {
    return "tone-success";
  }
  if (["warning", "warn", "stale", "missing", "open", "pending"].includes(normalized)) {
    return "tone-warning";
  }
  if (["danger", "fail", "failed", "error", "halted", "rejected", "false", "no"].includes(normalized)) {
    return "tone-danger";
  }
  if (normalized === "primary") {
    return "tone-primary";
  }
  return "";
}

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
