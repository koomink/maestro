import type { Tone } from "../../types";
import { formatValue } from "../../utils/format";
import { toneClass } from "../../utils/tone";

export function MiniFact({
  label,
  value,
  tone = "neutral",
  tooltip,
}: {
  label: string;
  value: unknown;
  tone?: Tone;
  tooltip?: string;
}) {
  return (
    <span className={"mini-fact " + toneClass(tone)} title={tooltip}>
      <span>{label}</span>
      <strong>{formatValue(value)}</strong>
    </span>
  );
}
