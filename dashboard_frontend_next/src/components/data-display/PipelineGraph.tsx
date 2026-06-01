import { useState } from "react";

import type { PipelineNode } from "../../types";
import { formatValue } from "../../utils/format";

function nodeUpdatedAt(node: PipelineNode): string {
  return formatValue(node.updated_at_display || node.updated_at || "n/a");
}

export function PipelineGraph({
  nodes,
  compact = false,
}: {
  nodes: PipelineNode[];
  compact?: boolean;
}) {
  const [selectedNodeId, setSelectedNodeId] = useState<string>(nodes[0]?.id || "");
  const selected = nodes.find((node) => node.id === selectedNodeId) || nodes[0];

  if (!nodes.length) {
    return <p className="muted-copy">No pipeline evidence is available in this snapshot.</p>;
  }

  return (
    <div className={compact ? "pipeline-graph compact" : "pipeline-graph"}>
      <div className="pipeline-track" role="list">
        {nodes.map((node, index) => (
          <button
            key={node.id}
            className={node.id === selected?.id ? `pipeline-node active tone-${node.tone || "neutral"}` : `pipeline-node tone-${node.tone || "neutral"}`}
            type="button"
            onClick={() => setSelectedNodeId(node.id)}
            role="listitem"
          >
            <span className="pipeline-step">{index + 1}</span>
            <strong>{node.label}</strong>
            <small>{formatValue(node.status)}</small>
          </button>
        ))}
      </div>

      {selected && !compact && (
        <div className={`pipeline-detail tone-${selected.tone || "neutral"}`}>
          <div>
            <span className="eyebrow">{selected.label}</span>
            <strong>{formatValue(selected.status)}</strong>
          </div>
          <p>{formatValue(selected.detail)}</p>
          <dl>
            <div>
              <dt>Updated</dt>
              <dd>{nodeUpdatedAt(selected)}</dd>
            </div>
            <div>
              <dt>Run ID</dt>
              <dd>{formatValue(selected.run_id || "n/a")}</dd>
            </div>
            <div>
              <dt>Next Check</dt>
              <dd>{formatValue(selected.next_check || "Review dashboard evidence.")}</dd>
            </div>
          </dl>
        </div>
      )}
    </div>
  );
}
