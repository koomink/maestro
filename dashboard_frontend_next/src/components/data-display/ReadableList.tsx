import type { Row } from "../../types";
import { firstValue } from "../../utils/data";
import { formatValue } from "../../utils/format";
import { toneClass } from "../../utils/tone";

export function ReadableList({
  empty,
  rows,
  titleKeys,
  detailKeys,
  limit,
}: {
  empty: string;
  rows: Row[];
  titleKeys: string[];
  detailKeys: string[];
  limit: number;
}) {
  if (!rows.length) {
    return <p className="muted-copy">{empty}</p>;
  }
  return (
    <div className="readable-list">
      {rows.slice(0, limit).map((row, index) => (
        <article className={`list-item ${toneClass(firstValue(row, ["tone", "status", "state"]))}`} key={index}>
          <strong>{formatValue(firstValue(row, titleKeys) || `Item ${index + 1}`)}</strong>
          <span>{formatValue(firstValue(row, detailKeys) || "No detail supplied.")}</span>
        </article>
      ))}
    </div>
  );
}
