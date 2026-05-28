import type { Row } from "../../types";
import { formatReadableCell } from "../../utils/format";
import { humanize } from "../../utils/format";

export function KeyValueRows({ row, keys }: { row: Row; keys: string[] }) {
  const visible = keys
    .map((key) => [key, row[key]] as const)
    .filter(([, value]) => value !== undefined && value !== null);
  if (!visible.length) {
    return <p className="muted-copy">No summary available.</p>;
  }
  return (
    <dl className="key-values">
      {visible.map(([key, value]) => (
        <div key={key}>
          <dt>{humanize(key)}</dt>
          <dd>{formatReadableCell(value)}</dd>
        </div>
      ))}
    </dl>
  );
}
