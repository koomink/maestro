import type { Tone } from "../../types";
import { toneClass } from "../../utils/tone";

export function ShellMessage({ title, copy, tone = "neutral" }: { title: string; copy: string; tone?: Tone }) {
  return (
    <main className="center-shell">
      <article className={`shell-card ${toneClass(tone)}`}>
        <span className="eyebrow">Symphony</span>
        <h1>{title}</h1>
        <p>{copy}</p>
      </article>
    </main>
  );
}
