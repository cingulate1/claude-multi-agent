import dagre from "dagre";
import { MarkerType, Position } from "@xyflow/react";
import { formatCompactNumber, normalizeDisplayState } from "./format";

const NODE_WIDTH = 308;
const AGENT_NODE_HEIGHT = 197;
const SCRIPT_NODE_HEIGHT = 120;

const ACTIVE_STATES = new Set(["thinking", "reading", "writing", "running"]);
const TERMINAL_STATES = new Set(["completed", "failed", "cancelled", "terminated"]);

function buildRunStatusMap(runStatus) {
  if (!runStatus?.rows?.length) {
    return {};
  }

  return Object.fromEntries(runStatus.rows.map((row) => [row.agent, row]));
}

function buildArtifactsMap(nodeArtifacts) {
  if (!nodeArtifacts) {
    return {};
  }
  return nodeArtifacts;
}

function buildDependents(nodes) {
  const dependents = {};
  for (const node of nodes) {
    dependents[node.name] ??= [];
    for (const dependency of node.depends_on ?? []) {
      dependents[dependency] ??= [];
      dependents[dependency].push(node.name);
    }
  }
  return dependents;
}

function getCycleInfo(nodeName, cycles = [], cycleStates = {}) {
  const matchingCycles = [];

  for (const cycle of cycles) {
    if (cycle.type === "self-loop" && cycle.agent === nodeName) {
      const state = cycleStates[nodeName] ?? {};
      matchingCycles.push({
        key: nodeName,
        kind: "self-loop",
        currentRound: state.current_round ?? 0,
        maxRounds: state.max_rounds ?? cycle.max_iterations ?? 0,
        state: normalizeDisplayState(state.state ?? "pending"),
      });
    }

    if (cycle.type === "bipartite" && (cycle.producer === nodeName || cycle.evaluator === nodeName)) {
      const key = `${cycle.producer}-${cycle.evaluator}`;
      const state = cycleStates[key] ?? {};
      matchingCycles.push({
        key,
        kind: "bipartite",
        currentRound: state.current_round ?? 0,
        maxRounds: state.max_rounds ?? cycle.max_rounds ?? 0,
        state: normalizeDisplayState(state.state ?? "pending"),
      });
    }
  }

  return matchingCycles;
}

function resolveDisplayState(orchestratorState, runStatusRow) {
  const baseState = normalizeDisplayState(orchestratorState ?? "pending");
  const sidecarState = normalizeDisplayState(runStatusRow?.state ?? "");

  if (TERMINAL_STATES.has(baseState)) {
    return baseState;
  }

  if (sidecarState && sidecarState !== "pending") {
    if (sidecarState === "completed") {
      return "completed";
    }
    return sidecarState;
  }

  return baseState;
}

function buildNodeData(node, snapshot, dependentsMap, selectedNodeId) {
  const statusNode = snapshot.status?.nodes?.[node.name] ?? {};
  const runStatusRow = snapshot.runStatusMap[node.name] ?? null;
  const artifactInfo = snapshot.artifactsMap[node.name] ?? { outputs: [] };
  const cycleInfo = getCycleInfo(node.name, snapshot.plan?.cycles, snapshot.status?.cycles);

  const displayState = resolveDisplayState(statusNode.state, runStatusRow);
  const contextTokens = Number(statusNode.tokens?.input ?? 0);
  const outputTokens = Number(statusNode.tokens?.output ?? 0);
  const outputCount = artifactInfo.outputs?.length ?? 0;
  const availableOutputs = artifactInfo.outputs?.filter((entry) => entry.exists).length ?? 0;

  const badgeLine = [];
  if (node.node_type === "script") {
    badgeLine.push("Script");
  } else {
    badgeLine.push(statusNode.model ?? snapshot.nodeModels?.[node.name] ?? "Unknown");
  }
  if (node.parallel_group) {
    badgeLine.push(`Group: ${node.parallel_group}`);
  }
  if (cycleInfo.length) {
    badgeLine.push(
      cycleInfo
        .map((cycle) =>
          cycle.maxRounds
            ? `${cycle.kind === "self-loop" ? "Loop" : "Cycle"} ${cycle.currentRound}/${cycle.maxRounds}`
            : cycle.kind === "self-loop"
              ? "Loop"
              : "Cycle",
        )
        .join(" · "),
    );
  }

  return {
    label: node.name,
    nodeType: node.full_agent ? "full_agent" : (node.node_type ?? "agent"),
    displayState,
    orchestratorState: normalizeDisplayState(statusNode.state ?? "pending"),
    activityState: normalizeDisplayState(runStatusRow?.state ?? ""),
    contextTokens,
    contextLabel: contextTokens > 0 ? formatCompactNumber(contextTokens) : "—",
    outputTokens,
    outputLabel: outputTokens > 0 ? formatCompactNumber(outputTokens) : "—",
    badgeLine,
    outputSummary: `${availableOutputs}/${outputCount}`,
    outputCount,
    availableOutputs,
    outputs: artifactInfo.outputs ?? [],
    dependencies: [...(node.depends_on ?? [])],
    dependents: [...(dependentsMap[node.name] ?? [])],
    selected: selectedNodeId === node.name,
    cycleInfo,
    startedAt: statusNode.started_at ?? null,
    completedAt: statusNode.completed_at ?? null,
    model: statusNode.model ?? snapshot.nodeModels?.[node.name] ?? "Unknown",
  };
}

function layoutNodes(nodes, edges, layoutDirection) {
  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({
    rankdir: layoutDirection,
    align: "UL",
    ranksep: layoutDirection === "LR" ? 130 : 150,
    nodesep: 48,
    marginx: 32,
    marginy: 32,
  });

  for (const node of nodes) {
    const nodeHeight = node.data.nodeType === "script" ? SCRIPT_NODE_HEIGHT : AGENT_NODE_HEIGHT;
    graph.setNode(node.id, { width: NODE_WIDTH, height: nodeHeight });
  }

  for (const edge of edges) {
    if (edge.data?.layout === false) {
      continue;
    }
    graph.setEdge(edge.source, edge.target);
  }

  dagre.layout(graph);

  return nodes.map((node) => {
    const nodeHeight = node.data.nodeType === "script" ? SCRIPT_NODE_HEIGHT : AGENT_NODE_HEIGHT;
    const positioned = graph.node(node.id) ?? { x: NODE_WIDTH / 2, y: nodeHeight / 2 };
    return {
      ...node,
      position: {
        x: positioned.x - NODE_WIDTH / 2,
        y: positioned.y - nodeHeight / 2,
      },
    };
  });
}

function buildNormalEdges(plan, nodeIndex) {
  const edges = [];

  for (const node of plan.nodes ?? []) {
    for (const dependency of node.depends_on ?? []) {
      edges.push({
        id: `${dependency}->${node.name}`,
        source: dependency,
        target: node.name,
        type: "smoothstep",
        animated: ACTIVE_STATES.has(nodeIndex[node.name]?.data.displayState),
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: "#315584",
          width: 20,
          height: 20,
        },
        style: {
          stroke: "#315584",
          strokeWidth: 2,
        },
      });
    }
  }

  return edges;
}

function buildCycleEdges(plan, cycleStates, nodeIndex) {
  const edges = [];

  for (const cycle of plan.cycles ?? []) {
    if (cycle.type === "self-loop") {
      const cycleState = cycleStates?.[cycle.agent] ?? {};
      edges.push({
        id: `${cycle.agent}-loop`,
        source: cycle.agent,
        target: cycle.agent,
        type: "loop",
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: "#8c6f1f",
        },
        data: {
          state: normalizeDisplayState(cycleState.state ?? "pending"),
          label:
            cycleState.current_round && cycleState.max_rounds
              ? `Loop ${cycleState.current_round}/${cycleState.max_rounds}`
              : cycle.max_iterations
                ? `Loop 0/${cycle.max_iterations}`
                : "Loop",
          layout: false,
        },
        animated: ACTIVE_STATES.has(nodeIndex[cycle.agent]?.data.displayState),
      });
    }

    if (cycle.type === "bipartite") {
      const key = `${cycle.producer}-${cycle.evaluator}`;
      const cycleState = cycleStates?.[key] ?? {};
      const label =
        cycleState.current_round && cycleState.max_rounds
          ? `Round ${cycleState.current_round}/${cycleState.max_rounds}`
          : cycle.max_rounds
            ? `Round 0/${cycle.max_rounds}`
            : "Cycle";

      edges.push({
        id: `${key}:forward`,
        source: cycle.producer,
        target: cycle.evaluator,
        type: "cycle",
        animated: ACTIVE_STATES.has(nodeIndex[cycle.producer]?.data.displayState),
        data: {
          direction: "forward",
          state: normalizeDisplayState(cycleState.state ?? "pending"),
          label,
          layout: false,
        },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: "#8c6f1f",
        },
      });

      edges.push({
        id: `${key}:reverse`,
        source: cycle.evaluator,
        target: cycle.producer,
        type: "cycle",
        animated: ACTIVE_STATES.has(nodeIndex[cycle.evaluator]?.data.displayState),
        data: {
          direction: "reverse",
          state: normalizeDisplayState(cycleState.state ?? "pending"),
          label,
          layout: false,
        },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: "#8c6f1f",
        },
      });
    }
  }

  return edges;
}

export function buildGraphSnapshot(rawSnapshot, options) {
  const plan = rawSnapshot?.plan;
  if (!plan?.nodes?.length) {
    return {
      nodes: [],
      edges: [],
      nodeLookup: {},
      stats: {
        total: 0,
        active: 0,
        completed: 0,
        failed: 0,
        compacted: 0,
      },
    };
  }

  const selectedNodeId = options.selectedNodeId ?? null;
  const layoutDirection = options.layoutDirection === "LR" ? "LR" : "TB";

  const runStatusMap = buildRunStatusMap(rawSnapshot.runStatus);
  const artifactsMap = buildArtifactsMap(rawSnapshot.nodeArtifacts);
  const dependentsMap = buildDependents(plan.nodes);

  const snapshot = {
    ...rawSnapshot,
    plan,
    runStatusMap,
    artifactsMap,
  };

  const baseNodes = plan.nodes.map((node) => {
    const nodeHeight = (node.node_type ?? "agent") === "script" ? SCRIPT_NODE_HEIGHT : AGENT_NODE_HEIGHT;
    return {
      id: node.name,
      type: "runNode",
      sourcePosition: layoutDirection === "LR" ? Position.Right : Position.Bottom,
      targetPosition: layoutDirection === "LR" ? Position.Left : Position.Top,
      data: buildNodeData(node, snapshot, dependentsMap, selectedNodeId),
      style: { width: NODE_WIDTH, height: nodeHeight },
    };
  });

  const nodeIndex = Object.fromEntries(baseNodes.map((node) => [node.id, node]));
  const dependencyEdges = buildNormalEdges(plan, nodeIndex);
  const cycleEdges = buildCycleEdges(plan, rawSnapshot.status?.cycles, nodeIndex);
  const edges = [...dependencyEdges, ...cycleEdges];
  const nodes = layoutNodes(baseNodes, dependencyEdges, layoutDirection);

  const stats = nodes.reduce(
    (accumulator, node) => {
      accumulator.total += 1;

      if (ACTIVE_STATES.has(node.data.displayState)) {
        accumulator.active += 1;
      }
      if (node.data.displayState === "completed") {
        accumulator.completed += 1;
      }
      if (node.data.displayState === "failed" || node.data.displayState === "cancelled" || node.data.displayState === "terminated") {
        accumulator.failed += 1;
      }
      if (node.data.displayState === "compacted" || node.data.activityState === "compacted") {
        accumulator.compacted += 1;
      }

      return accumulator;
    },
    { total: 0, active: 0, completed: 0, failed: 0, compacted: 0 },
  );

  return {
    nodes,
    edges,
    nodeLookup: Object.fromEntries(nodes.map((node) => [node.id, node])),
    stats,
  };
}

export function filterTimelineEvents(events, selectedNodeId, showHeartbeats) {
  const inputEvents = Array.isArray(events) ? events : [];

  return inputEvents
    .filter((event) => {
      if (!showHeartbeats && event.type === "heartbeat") {
        return false;
      }
      if (!selectedNodeId) {
        return true;
      }
      return event.agent === selectedNodeId;
    })
    .slice(-80)
    .reverse();
}
