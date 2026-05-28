import type { Tone } from "../../types";
import { formatValue } from "../../utils/format";
import { toneClass } from "../../utils/tone";

export function MiniFact({ label, value, tone = "neutral" }: { label: string; value: unknown; tone?: Tone }) {
  return (
    <span className={`mini-fact ${toneClass(tone)}`}>
      <span>{label}</span>
      <strong>{formatValue(value)}</strong>
    </span>
  );
}
