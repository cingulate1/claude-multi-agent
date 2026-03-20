import { Handle, Position } from "@xyflow/react";

function Metric({ label, value }) {
  return (
    <div className="node-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export default function GraphNode({ data, sourcePosition = Position.Bottom, targetPosition = Position.Top }) {
  return (
    <div
      className={[
        "graph-node-card",
        `state-${data.displayState}`,
        data.selected ? "is-selected" : "",
        data.dimmed ? "is-dimmed" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <Handle type="target" position={targetPosition} className="node-handle" />
      <div className="node-card-header">
        <span className="node-kind-pill">{data.nodeType === "script" ? "Script" : "Agent"}</span>
        <span className={`node-state-pill pill-${data.displayState}`}>{data.displayState}</span>
      </div>

      <div className="node-title">{data.label}</div>
      <div className="node-badges">
        {data.badgeLine.map((badge) => (
          <span key={badge} className="node-badge">
            {badge}
          </span>
        ))}
      </div>

      <div className="node-metrics-grid">
        <Metric label="Tokens" value={data.totalTokensLabel} />
        <Metric label="Outputs" value={data.outputSummary} />
        <Metric label="Read" value={data.filesRead ?? "—"} />
        <Metric label="Out KB" value={data.outputKb ?? "—"} />
      </div>

      {data.cycleInfo.length > 0 ? (
        <div className="node-cycle-row">
          {data.cycleInfo.map((cycle) => (
            <span key={cycle.key} className="node-cycle-badge">
              {cycle.kind === "self-loop" ? "Loop" : "Cycle"} {cycle.currentRound}/{cycle.maxRounds || "?"}
            </span>
          ))}
        </div>
      ) : null}

      <Handle type="source" position={sourcePosition} className="node-handle" />
    </div>
  );
}
