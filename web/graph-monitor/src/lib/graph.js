import dagre from "dagre";
import { MarkerType, Position } from "@xyflow/react";
import { formatCompactNumber, normalizeDisplayState } from "./format";

const NODE_WIDTH = 308;
const NODE_HEIGHT = 176;

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

function collectRelated(startName, adjacency) {
  const related = new Set();
  const queue = [startName];

  while (queue.length) {
    const current = queue.shift();
    for (const next of adjacency[current] ?? []) {
      if (related.has(next)) {
        continue;
      }
      related.add(next);
      queue.push(next);
    }
  }

  return related;
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

function buildNodeData(node, snapshot, dependentsMap, selectedNodeId, searchTerm, emphasizeActive) {
  const statusNode = snapshot.status?.nodes?.[node.name] ?? {};
  const runStatusRow = snapshot.runStatusMap[node.name] ?? null;
  const artifactInfo = snapshot.artifactsMap[node.name] ?? { outputs: [] };
  const cycleInfo = getCycleInfo(node.name, snapshot.plan?.cycles, snapshot.status?.cycles);

  const displayState = resolveDisplayState(statusNode.state, runStatusRow);
  const totalTokens = Number(statusNode.tokens?.input ?? 0) + Number(statusNode.tokens?.output ?? 0);
  const outputCount = artifactInfo.outputs?.length ?? 0;
  const availableOutputs = artifactInfo.outputs?.filter((entry) => entry.exists).length ?? 0;

  const matchesSearch =
    !searchTerm ||
    node.name.toLowerCase().includes(searchTerm) ||
    (statusNode.model ?? snapshot.nodeModels?.[node.name] ?? "").toLowerCase().includes(searchTerm) ||
    (node.parallel_group ?? "").toLowerCase().includes(searchTerm);

  const relatedToSelection =
    !selectedNodeId ||
    selectedNodeId === node.name ||
    snapshot.ancestorSet.has(node.name) ||
    snapshot.descendantSet.has(node.name);

  const active = ACTIVE_STATES.has(displayState);
  const dimmed =
    (!matchesSearch || !relatedToSelection || (emphasizeActive && !active)) &&
    selectedNodeId !== node.name;

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
    nodeType: node.node_type ?? "agent",
    displayState,
    orchestratorState: normalizeDisplayState(statusNode.state ?? "pending"),
    activityState: normalizeDisplayState(runStatusRow?.state ?? ""),
    totalTokens,
    totalTokensLabel: totalTokens > 0 ? formatCompactNumber(totalTokens) : "—",
    inputTokensLabel: runStatusRow?.tokensIn ?? (statusNode.tokens?.input ? formatCompactNumber(statusNode.tokens.input) : "—"),
    outputTokensLabel: runStatusRow?.tokensOut ?? (statusNode.tokens?.output ? formatCompactNumber(statusNode.tokens.output) : "—"),
    filesRead: runStatusRow?.filesRead ?? null,
    outputKb: runStatusRow?.outputKb ?? null,
    badgeLine,
    outputSummary: `${availableOutputs}/${outputCount}`,
    outputCount,
    availableOutputs,
    outputs: artifactInfo.outputs ?? [],
    dependencies: [...(node.depends_on ?? [])],
    dependents: [...(dependentsMap[node.name] ?? [])],
    selected: selectedNodeId === node.name,
    dimmed,
    matchesSearch,
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
    graph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }

  for (const edge of edges) {
    if (edge.data?.layout === false) {
      continue;
    }
    graph.setEdge(edge.source, edge.target);
  }

  dagre.layout(graph);

  return nodes.map((node) => {
    const positioned = graph.node(node.id) ?? { x: NODE_WIDTH / 2, y: NODE_HEIGHT / 2 };
    return {
      ...node,
      position: {
        x: positioned.x - NODE_WIDTH / 2,
        y: positioned.y - NODE_HEIGHT / 2,
      },
    };
  });
}

function buildNormalEdges(plan, nodeIndex, selectedNodeId, ancestorSet, descendantSet) {
  const edges = [];

  for (const node of plan.nodes ?? []) {
    for (const dependency of node.depends_on ?? []) {
      const connectedToSelection =
        !selectedNodeId ||
        dependency === selectedNodeId ||
        node.name === selectedNodeId ||
        (ancestorSet.has(dependency) && ancestorSet.has(node.name)) ||
        (descendantSet.has(dependency) && descendantSet.has(node.name)) ||
        ancestorSet.has(node.name) ||
        descendantSet.has(dependency);

      edges.push({
        id: `${dependency}->${node.name}`,
        source: dependency,
        target: node.name,
        type: "smoothstep",
        animated: ACTIVE_STATES.has(nodeIndex[node.name]?.data.displayState),
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: connectedToSelection ? "#315584" : "#8fa4bd",
        },
        style: {
          stroke: connectedToSelection ? "#315584" : "#8fa4bd",
          strokeWidth: connectedToSelection ? 2.1 : 1.4,
          opacity: connectedToSelection ? 0.95 : 0.42,
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
  const searchTerm = (options.searchTerm ?? "").trim().toLowerCase();
  const emphasizeActive = Boolean(options.emphasizeActive);
  const layoutDirection = options.layoutDirection === "LR" ? "LR" : "TB";

  const runStatusMap = buildRunStatusMap(rawSnapshot.runStatus);
  const artifactsMap = buildArtifactsMap(rawSnapshot.nodeArtifacts);
  const dependentsMap = buildDependents(plan.nodes);
  const selectedNode = selectedNodeId ? plan.nodes.find((node) => node.name === selectedNodeId) : null;
  const ancestorSet = selectedNode ? collectRelated(selectedNode.name, Object.fromEntries(plan.nodes.map((node) => [node.name, node.depends_on ?? []]))) : new Set();
  const descendantSet = selectedNode ? collectRelated(selectedNode.name, dependentsMap) : new Set();

  const snapshot = {
    ...rawSnapshot,
    plan,
    runStatusMap,
    artifactsMap,
    ancestorSet,
    descendantSet,
  };

  const baseNodes = plan.nodes.map((node) => ({
    id: node.name,
    type: "runNode",
    sourcePosition: layoutDirection === "LR" ? Position.Right : Position.Bottom,
    targetPosition: layoutDirection === "LR" ? Position.Left : Position.Top,
    data: buildNodeData(node, snapshot, dependentsMap, selectedNodeId, searchTerm, emphasizeActive),
    style: { width: NODE_WIDTH, height: NODE_HEIGHT },
  }));

  const nodeIndex = Object.fromEntries(baseNodes.map((node) => [node.id, node]));
  const dependencyEdges = buildNormalEdges(plan, nodeIndex, selectedNodeId, ancestorSet, descendantSet);
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
