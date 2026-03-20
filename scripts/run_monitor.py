#!/usr/bin/env python3
"""Sidecar monitor for multi-agent-graph execution runs.

Polls agent NDJSON log files every N seconds, extracts key signals, and
maintains two output files:
  - run-status.md  : compact markdown snapshot (overwritten each tick)
  - timeline.jsonl : append-only event log

Usage:
    python run_monitor.py --run-dir PATH [--interval SECONDS]
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_tokens(n: int) -> str:
    """Format a token count with K/M suffix."""
    if n >= 1_000_000:
        value = n / 1_000_000
        if value >= 10:
            return f"{value:.0f}M"
        return f"{value:.1f}M"
    if n >= 1_000:
        value = n / 1_000
        if value >= 10:
            return f"{value:.0f}K"
        return f"{value:.1f}K"
    return str(n)


def _fmt_elapsed(seconds: float) -> str:
    """Format seconds into a compact elapsed string like 4m12s or 1h03m."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h{mins:02d}m"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_ts() -> str:
    """Short timestamp for timeline events (HH:MM:SS)."""
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _sanitize_text(raw: str) -> str:
    r"""Decode JSON encoding artifacts into clean text.

    json.loads already handles standard JSON escaping (\n, \t, \", \uXXXX).
    This function catches any residual double-encoding that can occur when
    text passes through multiple serialization layers.
    """
    text = raw
    # Only fix genuinely double-escaped sequences (literal backslash + n in
    # the Python string, meaning the JSON contained \\n which decoded to \n
    # as a two-char sequence rather than a newline).
    if "\\" in text:
        text = text.replace("\\n", "\n")
        text = text.replace("\\t", "\t")
        text = text.replace('\\"', '"')
        # Decode double-escaped unicode like \\u2019 -> '
        text = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda m: chr(int(m.group(1), 16)),
            text,
        )
    return text


# ---------------------------------------------------------------------------
# Agent state
# ---------------------------------------------------------------------------

READING_TOOLS = {"Read", "Glob", "Grep"}
WRITING_TOOLS = {"Write", "Edit"}


class AgentState:
    """Tracks cumulative state for a single agent across one or more log files."""

    def __init__(self, name: str):
        self.name = name
        self.state = "pending"
        self.started_at: Optional[float] = None
        self.tokens_in = 0          # latest context window utilization (input + cache_creation + cache_read)
        self.tokens_out = 0         # cumulative output_tokens
        self.files_read = 0         # count of Read tool invocations
        self.compacted = False
        self.complete = False
        self.last_tool: Optional[str] = None
        self._pending_compaction = False  # awaiting compact_boundary for pre_tokens

        # Per-log-file seek tracking: path -> offset
        self.log_seeks: Dict[str, int] = {}

        # Track seen assistant message IDs to avoid double-counting tokens
        self._seen_msg_ids: Set[str] = set()

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.time() - self.started_at

    def detect_display_state(self) -> str:
        """Derive the display state from the most recent activity."""
        if self.state in ("complete", "failed", "compacted"):
            return self.state
        if self.compacted:
            return "compacted"
        if self.last_tool:
            if self.last_tool in READING_TOOLS:
                return "reading"
            if self.last_tool in WRITING_TOOLS:
                return "writing"
        if self.started_at is not None:
            return "thinking"
        return "pending"


# ---------------------------------------------------------------------------
# Log parser
# ---------------------------------------------------------------------------

class LogParser:
    """Parses new bytes from an agent's NDJSON log file(s)."""

    def parse_new_lines(
        self,
        log_path: Path,
        agent: AgentState,
    ) -> List[Dict[str, Any]]:
        """Read new lines from a log file, update agent state, return events.

        Tracks seek position per file so only new bytes are read each tick.
        """
        path_key = str(log_path)
        seek_pos = agent.log_seeks.get(path_key, 0)
        events: List[Dict[str, Any]] = []

        try:
            file_size = log_path.stat().st_size
        except OSError:
            return events

        if file_size <= seek_pos:
            return events

        try:
            with open(log_path, "r", encoding="utf-8") as f:
                f.seek(seek_pos)
                raw = f.read()
                new_pos = f.tell()
        except OSError:
            return events

        agent.log_seeks[path_key] = new_pos

        # Mark agent as started on first read
        if agent.started_at is None:
            agent.started_at = time.time()

        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue

            evts = self._process_line(obj, agent)
            events.extend(evts)

        return events

    def _process_line(
        self,
        obj: Dict[str, Any],
        agent: AgentState,
    ) -> List[Dict[str, Any]]:
        """Process a single NDJSON object and return any events to emit."""
        events: List[Dict[str, Any]] = []
        etype = obj.get("type", "")

        if etype == "system":
            events.extend(self._handle_system(obj, agent))

        elif etype == "assistant":
            events.extend(self._handle_assistant(obj, agent))

        elif etype == "result":
            events.extend(self._handle_result(obj, agent))

        return events

    def _handle_system(
        self,
        obj: Dict[str, Any],
        agent: AgentState,
    ) -> List[Dict[str, Any]]:
        """Handle system-type NDJSON lines.

        Compaction arrives as a three-line sequence:
          1. {"type":"system","subtype":"status","status":"compacting",...}
          2. {"type":"system","subtype":"status","status":null,...}
          3. {"type":"system","subtype":"compact_boundary","compact_metadata":{"pre_tokens":N}}

        We defer emitting the compaction event until line 3 so we can
        include pre_tokens.  If compact_boundary never arrives, the
        pending flag is still set and compacted=True on the agent state.
        """
        events: List[Dict[str, Any]] = []
        subtype = obj.get("subtype", "")

        if subtype == "status" and obj.get("status") == "compacting":
            agent.compacted = True
            agent._pending_compaction = True

        elif subtype == "compact_boundary":
            metadata = obj.get("compact_metadata", {})
            pre_tokens = metadata.get("pre_tokens", 0)
            agent.compacted = True
            agent._pending_compaction = False
            events.append({
                "ts": _now_ts(),
                "agent": agent.name,
                "type": "compaction",
                "pre_tokens": pre_tokens,
                "in": agent.tokens_in,
                "out": agent.tokens_out,
            })

        return events

    def _handle_assistant(
        self,
        obj: Dict[str, Any],
        agent: AgentState,
    ) -> List[Dict[str, Any]]:
        """Handle assistant-type NDJSON lines."""
        events: List[Dict[str, Any]] = []
        message = obj.get("message", {})

        # Token accounting — deduplicate by message ID
        msg_id = message.get("id", "")
        usage = message.get("usage", {})
        if msg_id and msg_id not in agent._seen_msg_ids:
            agent._seen_msg_ids.add(msg_id)
            # Context window utilization = sum of all three mutually exclusive partitions
            agent.tokens_in = (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )
            agent.tokens_out += usage.get("output_tokens", 0)

        # Process content blocks
        content = message.get("content", [])
        for block in content:
            btype = block.get("type", "")

            if btype == "tool_use":
                tool_name = block.get("name", "")
                agent.last_tool = tool_name

                if tool_name == "Read":
                    agent.files_read += 1

                target = self._extract_tool_target(tool_name, block.get("input", {}))
                events.append({
                    "ts": _now_ts(),
                    "agent": agent.name,
                    "type": "tool_use",
                    "tool": tool_name,
                    "target": target,
                    "in": agent.tokens_in,
                    "out": agent.tokens_out,
                })

            elif btype == "text":
                raw_text = block.get("text", "")
                if raw_text.strip():
                    sanitized = _sanitize_text(raw_text)
                    events.append({
                        "ts": _now_ts(),
                        "agent": agent.name,
                        "type": "message",
                        "text": sanitized,
                        "in": agent.tokens_in,
                        "out": agent.tokens_out,
                    })

        return events

    def _handle_result(
        self,
        obj: Dict[str, Any],
        agent: AgentState,
    ) -> List[Dict[str, Any]]:
        """Handle result-type NDJSON lines (final event in a log)."""
        subtype = obj.get("subtype", "")
        is_error = obj.get("is_error", False)

        # Update tokens from result usage (authoritative final counts)
        usage = obj.get("usage", {})
        result_in = (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
        result_out = usage.get("output_tokens", 0)
        if result_in > 0:
            agent.tokens_in = result_in
        if result_out > 0:
            agent.tokens_out = result_out

        if subtype == "success" and not is_error:
            agent.state = "complete"
            agent.complete = True
        else:
            agent.state = "failed"

        return []

    @staticmethod
    def _extract_tool_target(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Extract a concise target description from tool input."""
        if tool_name in ("Read", "Write"):
            return tool_input.get("file_path", "")
        if tool_name == "Grep":
            return tool_input.get("pattern", "")
        if tool_name == "Glob":
            return tool_input.get("pattern", "")
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            return cmd[:100] if len(cmd) > 100 else cmd
        if tool_name == "Edit":
            return tool_input.get("file_path", "")
        # Generic fallback
        for key in ("file_path", "path", "pattern", "command", "query"):
            if key in tool_input:
                val = str(tool_input[key])
                return val[:100] if len(val) > 100 else val
        return ""


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class RunMonitor:
    """Main monitor loop."""

    def __init__(self, run_dir: Path, interval: float = 3.0):
        self.run_dir = run_dir.resolve()
        self.log_dir = self.run_dir / "logs"
        self.output_dir = self.run_dir / "output"
        self.interval = interval
        self.start_time = time.time()

        self.agents: Dict[str, AgentState] = {}
        self.agent_names: List[str] = []
        self.parser = LogParser()

        self.status_path = self.log_dir / "run-status.md"
        self.timeline_path = self.log_dir / "timeline.jsonl"

        self._stop = False
        self._idle_since: Optional[float] = None

    def discover_agents(self) -> None:
        """Read execution_plan.json to get the list of agent names."""
        plan_path = self.run_dir / "execution_plan.json"
        if not plan_path.exists():
            print(f"[monitor] Waiting for execution_plan.json at {plan_path}",
                  file=sys.stderr)
            return

        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[monitor] Failed to read execution plan: {e}",
                  file=sys.stderr)
            return

        nodes = plan.get("nodes", [])
        for node in nodes:
            name = node.get("name", "")
            if name and name not in self.agents:
                self.agents[name] = AgentState(name)
                self.agent_names.append(name)

    def find_log_files(self, agent_name: str) -> List[Path]:
        """Find all log files for an agent.

        Agents may have:
          - {name}.log                 (simple run)
          - {name}-iter{N}.log         (self-loop iterations)
          - {name}-r{N}.log            (bipartite cycle rounds)
        """
        if not self.log_dir.exists():
            return []

        # Build a regex that matches:
        #   {name}.log
        #   {name}-iter{N}.log   (self-loop)
        #   {name}-r{N}.log      (bipartite round)
        escaped = re.escape(agent_name)
        pattern = re.compile(rf"^{escaped}(?:-(?:iter|r)\d+)?\.log$")

        files: List[Path] = []
        for p in self.log_dir.iterdir():
            if p.is_file() and pattern.match(p.name):
                files.append(p)

        return sorted(files)

    def get_output_kb(self, agent_name: str) -> Optional[float]:
        """Get the size of the agent's output file in KB, if it exists."""
        output_path = self.output_dir / f"{agent_name}.md"
        if output_path.exists():
            try:
                size_bytes = output_path.stat().st_size
                return size_bytes / 1024.0
            except OSError:
                pass
        return None

    def tick(self) -> int:
        """Run one polling cycle. Returns the number of new events found."""
        # Re-discover agents in case dynamic templates expanded the plan
        self.discover_agents()

        all_events: List[Dict[str, Any]] = []

        for name in self.agent_names:
            agent = self.agents[name]
            log_files = self.find_log_files(name)

            for log_path in log_files:
                events = self.parser.parse_new_lines(log_path, agent)
                all_events.extend(events)

            # Check process exit status from log files
            self._check_exit_status(agent, log_files)

        # Write timeline events
        if all_events:
            self._append_timeline(all_events)
            self._idle_since = None
        else:
            # No new events — emit heartbeat
            heartbeat = {
                "ts": _now_ts(),
                "type": "heartbeat",
                "events": 0,
            }
            self._append_timeline([heartbeat])
            if self._idle_since is None:
                self._idle_since = time.time()

        # Write status snapshot
        self._write_status()

        return len(all_events)

    def _check_exit_status(self, agent: AgentState, log_files: List[Path]) -> None:
        """Check if agent exited without a result event (crash/kill).

        The parser sets agent.state to "complete" or "failed" when it
        encounters a result line. If the log stopped growing without one,
        the orchestrator's status.json is the authoritative source —
        this monitor just reflects what it sees in the logs.
        """
        pass

    def _write_status(self) -> None:
        """Write the run-status.md snapshot."""
        elapsed = _fmt_elapsed(time.time() - self.start_time)
        now = _now_iso()

        lines = [
            "# Run Status",
            f"Updated: {now} | Elapsed: {elapsed}",
            "",
            "| Agent | State | Elapsed | Compacted | Tokens In | Tokens Out | Files Read | Output KB | Complete |",
            "|-------|-------|---------|-----------|-----------|------------|------------|-----------|----------|",
        ]

        for name in self.agent_names:
            agent = self.agents[name]
            state = agent.detect_display_state()
            started = agent.started_at is not None
            dash = "\u2014"

            ag_elapsed = _fmt_elapsed(agent.elapsed_seconds) if started else dash
            compacted = "yes" if agent.compacted else "no"
            tok_in = _fmt_tokens(agent.tokens_in) if started else dash
            tok_out = _fmt_tokens(agent.tokens_out) if started else dash
            files_read = str(agent.files_read) if started else dash
            output_kb = self.get_output_kb(name)
            output_str = f"{output_kb:.0f}" if output_kb is not None else dash
            complete = "yes" if agent.complete else "no"

            lines.append(
                f"| {name} | {state} | {ag_elapsed} | {compacted} | "
                f"{tok_in} | {tok_out} | {files_read} | {output_str} | {complete} |"
            )

        content = "\n".join(lines) + "\n"

        # Atomic write
        tmp_path = self.status_path.with_suffix(".md.tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            # On Windows, replace() fails if target exists in some cases.
            # Use a remove-then-rename pattern.
            try:
                if self.status_path.exists():
                    self.status_path.unlink()
            except OSError:
                pass
            try:
                tmp_path.rename(self.status_path)
            except OSError:
                # Fallback: direct write
                self.status_path.write_text(content, encoding="utf-8")
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        except OSError as e:
            print(f"[monitor] Failed to write status: {e}", file=sys.stderr)

    def _append_timeline(self, events: List[Dict[str, Any]]) -> None:
        """Append events to the timeline JSONL file."""
        try:
            with open(self.timeline_path, "a", encoding="utf-8") as f:
                for evt in events:
                    line = json.dumps(evt, ensure_ascii=False, separators=(",", ":"))
                    f.write(line + "\n")
        except OSError as e:
            print(f"[monitor] Failed to write timeline: {e}", file=sys.stderr)

    def all_finished(self) -> bool:
        """Check if all agents are in a terminal state."""
        if not self.agents:
            return False
        return all(
            a.state in ("complete", "failed")
            for a in self.agents.values()
        )

    def idle_timeout_reached(self, timeout: float = 30.0) -> bool:
        """Check if we've been idle (no new events) for longer than timeout."""
        if self._idle_since is None:
            return False
        return (time.time() - self._idle_since) >= timeout

    def run(self) -> None:
        """Main loop. Runs until interrupted or all agents are done."""
        print(f"[monitor] Monitoring {self.run_dir}", file=sys.stderr)
        print(f"[monitor] Polling every {self.interval}s", file=sys.stderr)
        print(f"[monitor] Status: {self.status_path}", file=sys.stderr)
        print(f"[monitor] Timeline: {self.timeline_path}", file=sys.stderr)

        # Initial discovery
        self.discover_agents()
        if self.agents:
            print(f"[monitor] Found {len(self.agents)} agent(s): "
                  f"{', '.join(self.agent_names)}", file=sys.stderr)
        else:
            print("[monitor] No agents found yet, will retry each tick",
                  file=sys.stderr)

        while not self._stop:
            try:
                event_count = self.tick()

                if event_count > 0:
                    print(f"[monitor] Tick: {event_count} event(s)", file=sys.stderr)

                # Auto-exit condition
                if self.all_finished() and self.idle_timeout_reached(30.0):
                    print("[monitor] All agents finished, idle timeout reached. Exiting.",
                          file=sys.stderr)
                    break

                time.sleep(self.interval)

            except KeyboardInterrupt:
                break

        print("[monitor] Shutting down.", file=sys.stderr)

    def stop(self) -> None:
        """Signal the monitor to stop."""
        self._stop = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sidecar monitor for multi-agent-graph runs"
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Path to the run directory",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="Polling interval in seconds (default: 3)",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"Error: run directory does not exist: {run_dir}", file=sys.stderr)
        sys.exit(1)

    monitor = RunMonitor(run_dir, interval=args.interval)

    # Handle graceful shutdown
    def _signal_handler(signum: int, frame: Any) -> None:
        print(f"\n[monitor] Received signal {signum}, stopping...", file=sys.stderr)
        monitor.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)

    monitor.run()


if __name__ == "__main__":
    main()
