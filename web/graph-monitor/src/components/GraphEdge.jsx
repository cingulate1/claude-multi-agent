import { BaseEdge, EdgeLabelRenderer, getBezierPath } from "@xyflow/react";

function EdgeLabel({ x, y, label }) {
  if (!label) {
    return null;
  }

  return (
    <EdgeLabelRenderer>
      <div
        className="edge-label"
        style={{
          transform: `translate(-50%, -50%) translate(${x}px, ${y}px)`,
        }}
      >
        {label}
      </div>
    </EdgeLabelRenderer>
  );
}

export function CycleEdge(props) {
  const color = props.data?.state === "failed" ? "#a73c36" : "#9b7b18";
  const curvature = props.data?.direction === "reverse" ? -0.34 : 0.34;
  const [path, labelX, labelY] = getBezierPath({
    sourceX: props.sourceX,
    sourceY: props.sourceY,
    sourcePosition: props.sourcePosition,
    targetX: props.targetX,
    targetY: props.targetY,
    targetPosition: props.targetPosition,
    curvature,
  });

  return (
    <>
      <BaseEdge
        id={props.id}
        path={path}
        markerEnd={props.markerEnd}
        style={{
          stroke: color,
          strokeWidth: 1.8,
          strokeDasharray: "7 5",
          opacity: props.animated ? 0.95 : 0.72,
        }}
      />
      <EdgeLabel x={labelX} y={labelY} label={props.data?.direction === "forward" ? props.data?.label : ""} />
    </>
  );
}

export function LoopEdge(props) {
  const color = props.data?.state === "failed" ? "#a73c36" : "#9b7b18";
  const loopHeight = 92;
  const loopWidth = 70;
  const path = [
    `M ${props.sourceX} ${props.sourceY}`,
    `C ${props.sourceX + loopWidth} ${props.sourceY - loopHeight}`,
    `${props.targetX - loopWidth} ${props.targetY - loopHeight}`,
    `${props.targetX} ${props.targetY}`,
  ].join(" ");

  return (
    <>
      <BaseEdge
        id={props.id}
        path={path}
        markerEnd={props.markerEnd}
        style={{
          stroke: color,
          strokeWidth: 1.8,
          strokeDasharray: "7 5",
          opacity: props.animated ? 0.95 : 0.72,
        }}
      />
      <EdgeLabel x={props.sourceX} y={props.sourceY - loopHeight - 12} label={props.data?.label} />
    </>
  );
}
