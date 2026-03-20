import { formatIsoTimestamp } from "../lib/format";

function StatCard({ label, value, accent }) {
  return (
    <div className={`stat-card accent-${accent}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export default function SummaryStrip({ snapshot, stats }) {
  const state = snapshot?.status?.state ?? "waiting";
  const createdAt = snapshot?.status?.created_at ?? null;
  const updatedAt = snapshot?.status?.updated_at ?? null;

  return (
    <header className="summary-strip">
      <div className="summary-title-block">
        <div className="summary-kicker">multi-agent-graph monitor</div>
        <h1>{snapshot?.plan?.pattern ?? "Waiting for execution plan"}</h1>
        <div className="summary-meta-line">
          <span className={`summary-state-pill pill-${state}`}>{state}</span>
          <span>{snapshot?.meta?.dataMode ?? "plan-only"}</span>
          {snapshot?.meta?.stale ? <span>Stale after {Math.round(snapshot.meta.staleSeconds)}s</span> : null}
        </div>
        <div className="summary-run-dir">{snapshot?.runDir ?? ""}</div>
      </div>

      <div className="summary-stat-grid">
        <StatCard label="Nodes" value={stats.total} accent="slate" />
        <StatCard label="Active" value={stats.active} accent="blue" />
        <StatCard label="Done" value={stats.completed} accent="green" />
        <StatCard label="Failed" value={stats.failed} accent="red" />
        <StatCard label="Compacted" value={stats.compacted} accent="amber" />
        <StatCard label="Updated" value={formatIsoTimestamp(updatedAt)} accent="slate" />
      </div>

      <div className="summary-activity">
        <div className="summary-activity-label">Current activity</div>
        <div className="summary-activity-value">{snapshot?.status?.activity ?? "Waiting for status updates"}</div>
        <div className="summary-activity-meta">
          Created {formatIsoTimestamp(createdAt)}
        </div>
      </div>
    </header>
  );
}
