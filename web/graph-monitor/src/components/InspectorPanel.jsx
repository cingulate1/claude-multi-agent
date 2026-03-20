import { formatBytes, formatIsoTimestamp } from "../lib/format";

function OutputRow({ output, previewPath, onPreview }) {
  return (
    <div className="inspector-output-row">
      <div>
        <div className="inspector-output-path">{output.path}</div>
        <div className="inspector-output-meta">
          {output.exists ? `Present · ${formatBytes(output.sizeBytes)}` : "Missing"}
        </div>
      </div>
      {output.exists ? (
        <button
          type="button"
          className={previewPath === output.path ? "preview-button is-active" : "preview-button"}
          onClick={() => onPreview(output.path)}
        >
          Preview
        </button>
      ) : null}
    </div>
  );
}

function EventRow({ event, onSelectAgent }) {
  return (
    <button type="button" className="inspector-event-row" onClick={() => event.agent && onSelectAgent(event.agent)}>
      <span className="timeline-event-time">{event.ts}</span>
      <div>
        <div className="timeline-event-title">
          {event.agent ? `${event.agent} · ${event.type}` : event.type}
        </div>
        <div className="timeline-event-body">
          {event.text ?? event.tool ?? event.target ?? "No payload"}
        </div>
      </div>
    </button>
  );
}

export default function InspectorPanel({
  selectedNode,
  timeline,
  preview,
  previewPath,
  onPreview,
  onSelectNode,
  finalOutput,
}) {
  if (!selectedNode) {
    return (
      <section className="panel inspector-panel">
        <div className="panel-heading">
          <div>
            <div className="panel-kicker">Inspector</div>
            <h2>Select a node</h2>
          </div>
        </div>
        <p className="panel-empty-copy">
          Click a node in the graph to inspect its dependencies, outputs, timestamps, and recent activity.
        </p>
        {finalOutput?.relativePath ? (
          <div className="panel-callout">
            <div className="panel-callout-label">Final Output</div>
            <div className="panel-callout-value">{finalOutput.relativePath}</div>
            <div className="panel-callout-meta">
              {finalOutput.exists ? "Available" : "Not written yet"}
            </div>
          </div>
        ) : null}
      </section>
    );
  }

  const recentEvents = timeline.slice(0, 8);

  return (
    <section className="panel inspector-panel">
      <div className="panel-heading">
        <div>
          <div className="panel-kicker">Inspector</div>
          <h2>{selectedNode.data.label}</h2>
        </div>
        <span className={`node-state-pill pill-${selectedNode.data.displayState}`}>
          {selectedNode.data.displayState}
        </span>
      </div>

      <div className="inspector-facts">
        <div>
          <span>Type</span>
          <strong>{selectedNode.data.nodeType}</strong>
        </div>
        <div>
          <span>Model</span>
          <strong>{selectedNode.data.model}</strong>
        </div>
        <div>
          <span>Started</span>
          <strong>{formatIsoTimestamp(selectedNode.data.startedAt)}</strong>
        </div>
        <div>
          <span>Finished</span>
          <strong>{formatIsoTimestamp(selectedNode.data.completedAt)}</strong>
        </div>
      </div>

      <div className="inspector-section">
        <div className="inspector-section-title">Dependencies</div>
        <div className="inspector-chip-row">
          {selectedNode.data.dependencies.length ? (
            selectedNode.data.dependencies.map((dependency) => (
              <button key={dependency} type="button" className="inspector-chip" onClick={() => onSelectNode(dependency)}>
                {dependency}
              </button>
            ))
          ) : (
            <span className="inspector-muted">No dependencies</span>
          )}
        </div>
      </div>

      <div className="inspector-section">
        <div className="inspector-section-title">Dependents</div>
        <div className="inspector-chip-row">
          {selectedNode.data.dependents.length ? (
            selectedNode.data.dependents.map((dependent) => (
              <button key={dependent} type="button" className="inspector-chip" onClick={() => onSelectNode(dependent)}>
                {dependent}
              </button>
            ))
          ) : (
            <span className="inspector-muted">No downstream nodes</span>
          )}
        </div>
      </div>

      <div className="inspector-section">
        <div className="inspector-section-title">Outputs</div>
        {selectedNode.data.outputs.length ? (
          <div className="inspector-output-list">
            {selectedNode.data.outputs.map((output) => (
              <OutputRow key={output.path} output={output} previewPath={previewPath} onPreview={onPreview} />
            ))}
          </div>
        ) : (
          <span className="inspector-muted">No declared outputs</span>
        )}
      </div>

      {previewPath && preview ? (
        <div className="inspector-section">
          <div className="inspector-section-title">Preview</div>
          <div className="preview-meta">{preview.path}</div>
          <pre className="preview-body">{preview.content}</pre>
        </div>
      ) : null}

      <div className="inspector-section">
        <div className="inspector-section-title">Recent Activity</div>
        {recentEvents.length ? (
          <div className="inspector-event-list">
            {recentEvents.map((event, index) => (
              <EventRow key={`${event.ts}-${index}`} event={event} onSelectAgent={onSelectNode} />
            ))}
          </div>
        ) : (
          <span className="inspector-muted">No timeline events for this node yet</span>
        )}
      </div>
    </section>
  );
}
