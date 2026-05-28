import type { Metric } from "../../types";
import { formatValue } from "../../utils/format";
import { toneClass } from "../../utils/tone";

export function MetricGrid({ metrics, featured = false }: { metrics: Metric[]; featured?: boolean }) {
  return (
    <div className={featured ? "metric-grid featured" : "metric-grid"}>
      {metrics.map((metric) => (
        <article className={`metric ${toneClass(metric.tone)}`} key={metric.label}>
          <span>{metric.label}</span>
          <strong>{formatValue(metric.value)}</strong>
        </article>
      ))}
    </div>
  );
}
