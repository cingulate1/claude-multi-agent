import { startTransition, useDeferredValue, useEffect, useMemo, useState } from "react";
import { Background, Controls, MiniMap, ReactFlow } from "@xyflow/react";
import GraphNode from "./components/GraphNode";
import { CycleEdge, LoopEdge } from "./components/GraphEdge";
import InspectorPanel from "./components/InspectorPanel";
import SummaryStrip from "./components/SummaryStrip";
import TimelinePanel from "./components/TimelinePanel";
import { buildGraphSnapshot, filterTimelineEvents } from "./lib/graph";

const POLL_INTERVAL_MS = 1500;

const nodeTypes = {
  runNode: GraphNode,
};

const edgeTypes = {
  cycle: CycleEdge,
  loop: LoopEdge,
};

function useSnapshotPolling() {
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    let timer = null;

    const poll = async () => {
      try {
        const response = await fetch("/api/snapshot", { cache: "no-store" });
        if (!response.ok) {
          throw new Error(`Snapshot request failed (${response.status})`);
        }

        const payload = await response.json();
        if (cancelled) {
          return;
        }

        startTransition(() => {
          setSnapshot(payload);
          setError("");
          setLoading(false);
        });

        if (!payload?.meta?.terminal) {
          timer = window.setTimeout(poll, POLL_INTERVAL_MS);
        }
      } catch (caughtError) {
        if (cancelled) {
          return;
        }

        setError(caughtError.message);
        setLoading(false);
        timer = window.setTimeout(poll, POLL_INTERVAL_MS * 2);
      }
    };

    poll();

    return () => {
      cancelled = true;
      if (timer) {
        window.clearTimeout(timer);
      }
    };
  }, []);

  return { snapshot, loading, error };
}

function usePreview(path) {
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;

    if (!path) {
      setPreview(null);
      setLoading(false);
      setError("");
      return undefined;
    }

    const loadPreview = async () => {
      setLoading(true);
      setError("");
      try {
        const response = await fetch(`/api/file?path=${encodeURIComponent(path)}`, { cache: "no-store" });
        if (!response.ok) {
          throw new Error(`Preview request failed (${response.status})`);
        }
        const payload = await response.json();
        if (!cancelled) {
          setPreview(payload);
          setLoading(false);
        }
      } catch (caughtError) {
        if (!cancelled) {
          setError(caughtError.message);
          setLoading(false);
        }
      }
    };

    loadPreview();

    return () => {
      cancelled = true;
    };
  }, [path]);

  return { preview, loading, error };
}

export default function App() {
  const { snapshot, loading, error } = useSnapshotPolling();
  const [layoutDirection, setLayoutDirection] = useState("TB");
  const [searchTerm, setSearchTerm] = useState("");
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [emphasizeActive, setEmphasizeActive] = useState(false);
  const [showHeartbeats, setShowHeartbeats] = useState(false);
  const [previewPath, setPreviewPath] = useState("");
  const deferredSearch = useDeferredValue(searchTerm);

  useEffect(() => {
    if (!snapshot?.plan?.nodes?.length) {
      setSelectedNodeId(null);
      return;
    }

    if (!selectedNodeId || !snapshot.plan.nodes.some((node) => node.name === selectedNodeId)) {
      setSelectedNodeId(snapshot.plan.nodes[0].name);
    }
  }, [selectedNodeId, snapshot]);

  useEffect(() => {
    if (!selectedNodeId) {
      setPreviewPath("");
    }
  }, [selectedNodeId]);

  const graph = useMemo(
    () =>
      buildGraphSnapshot(snapshot, {
        layoutDirection,
        searchTerm: deferredSearch,
        selectedNodeId,
        emphasizeActive,
      }),
    [deferredSearch, emphasizeActive, layoutDirection, selectedNodeId, snapshot],
  );

  const previewState = usePreview(previewPath);
  const selectedNode = selectedNodeId ? graph.nodeLookup[selectedNodeId] : null;
  const timeline = useMemo(
    () => filterTimelineEvents(snapshot?.timeline, selectedNodeId, showHeartbeats),
    [selectedNodeId, showHeartbeats, snapshot?.timeline],
  );

  return (
    <div className="app-shell">
      <div className="app-background" />
      <SummaryStrip snapshot={snapshot} stats={graph.stats} />

      <main className="app-grid">
        <section className="panel graph-panel">
          <div className="panel-heading">
            <div>
              <div className="panel-kicker">Graph</div>
              <h2>Execution topology</h2>
            </div>
            <div className="graph-toolbar">
              <input
                type="search"
                value={searchTerm}
                onChange={(event) => setSearchTerm(event.target.value)}
                placeholder="Search nodes, groups, or models"
                className="graph-search"
              />
              <button type="button" className="subtle-button" onClick={() => setLayoutDirection((current) => (current === "TB" ? "LR" : "TB"))}>
                {layoutDirection === "TB" ? "Left-to-right" : "Top-to-bottom"}
              </button>
              <button type="button" className={emphasizeActive ? "subtle-button is-active" : "subtle-button"} onClick={() => setEmphasizeActive((current) => !current)}>
                Emphasize active
              </button>
            </div>
          </div>

          {error ? <div className="banner banner-error">{error}</div> : null}

          <div className="graph-canvas-shell">
            {loading && !snapshot ? <div className="graph-overlay">Loading run data…</div> : null}
            {!snapshot?.plan?.nodes?.length ? (
              <div className="graph-overlay">Waiting for `execution_plan.json`…</div>
            ) : (
              <ReactFlow
                key={`${layoutDirection}-${snapshot?.meta?.planMtimeNs ?? 0}`}
                nodes={graph.nodes}
                edges={graph.edges}
                nodeTypes={nodeTypes}
                edgeTypes={edgeTypes}
                fitView
                fitViewOptions={{ padding: 0.14, maxZoom: 1.2 }}
                onNodeClick={(_, node) => setSelectedNodeId(node.id)}
                proOptions={{ hideAttribution: true }}
                minZoom={0.2}
                maxZoom={2}
              >
                <Background color="#cad3df" gap={18} size={1} />
                <MiniMap
                  pannable
                  zoomable
                  className="graph-minimap"
                  nodeColor={(node) => {
                    const displayState = node.data?.displayState ?? "pending";
                    if (displayState === "failed" || displayState === "cancelled" || displayState === "terminated") {
                      return "#d65245";
                    }
                    if (displayState === "completed") {
                      return "#2e8b65";
                    }
                    if (displayState === "writing") {
                      return "#8c6f1f";
                    }
                    if (displayState === "reading") {
                      return "#2f6f78";
                    }
                    return "#3f6aa1";
                  }}
                />
                <Controls showInteractive={false} />
              </ReactFlow>
            )}
          </div>
        </section>

        <div className="sidebar-stack">
          <InspectorPanel
            selectedNode={selectedNode}
            timeline={timeline}
            previewPath={previewPath}
            preview={
              previewState.loading
                ? { path: previewPath, content: "Loading preview…" }
                : previewState.error
                  ? { path: previewPath, content: previewState.error }
                  : previewState.preview
            }
            onPreview={setPreviewPath}
            onSelectNode={setSelectedNodeId}
            finalOutput={snapshot?.finalOutput}
          />
          <TimelinePanel
            events={timeline}
            selectedNodeId={selectedNodeId}
            showHeartbeats={showHeartbeats}
            onToggleHeartbeats={() => setShowHeartbeats((current) => !current)}
            onSelectNode={setSelectedNodeId}
          />
        </div>
      </main>
    </div>
  );
}
