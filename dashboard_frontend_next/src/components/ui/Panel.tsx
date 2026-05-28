import type { ReactNode } from "react";

export function Panel({
  title,
  eyebrow,
  children,
}: {
  title: string;
  eyebrow: string;
  children: ReactNode;
}) {
  return (
    <article className="panel">
      <span className="eyebrow">{eyebrow}</span>
      <h2>{title}</h2>
      {children}
    </article>
  );
}
