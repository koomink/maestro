import type { Tone } from "../../types";
import { formatValue } from "../../utils/format";
import { toneClass } from "../../utils/tone";

export function Signal({ label, value, tone }: { label: string; value: unknown; tone: Tone }) {
  return (
    <div className={`signal ${toneClass(tone)}`}>
      <span>{label}</span>
      <strong>{formatValue(value)}</strong>
    </div>
  );
}
