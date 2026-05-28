import type { Row } from "../../types";
import { columnsFor } from "../../utils/data";
import { formatReadableCell } from "../../utils/format";
import { humanize } from "../../utils/format";

export function ReadableTable({ rows, limit = 6 }: { rows: Row[]; limit?: number }) {
  if (!rows.length) {
    return <p className="muted-copy">No rows available.</p>;
  }
  const columns = columnsFor(rows).slice(0, 6);
  return (
    <div className="readable-table-wrap">
      <table className="readable-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{humanize(column)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, limit).map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={column}>{formatReadableCell(row[column])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
