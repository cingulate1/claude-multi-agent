#!/usr/bin/env python3
"""Status tracking for multi-agent-graph execution runs.

Writes a JSON status document that the graph monitor GUI polls for live updates.
Thread-safe: multiple callers can update concurrently.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from shared import agent_path_candidates, normalize_model_label, read_agent_frontmatter


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class RunStatusTracker:
    """Tracks execution state for a single pattern run."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir).resolve()
        self.log_dir = self.run_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / "status.json"
        self._lock = threading.Lock()
        self.data = self._load_existing() or self._new_status()
        self._write_locked()

        # Live token polling
        self._active_logs: Dict[str, List[Path]] = {}
        self._poller_stop = threading.Event()
        self._poller_thread: Optional[threading.Thread] = None

    def _load_existing(self) -> Optional[Dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _new_status(self) -> Dict[str, Any]:
        created = _now_iso()
        return {
            "schema_version": 1,
            "run_dir": str(self.run_dir),
            "created_at": created,
            "updated_at": created,
            "state": "idle",
            "pattern": None,
            "activity": "Initializing",
            "nodes": {},
            "cycles": {},
            "errors": [],
            "events": [],
            "final_output": None,
        }

    def _touch(self) -> None:
        self.data["updated_at"] = _now_iso()

    def _write_locked(self) -> None:
        self._touch()
        tmp_path = self.path.with_suffix(".tmp")
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        text = json.dumps(self.data, indent=2, ensure_ascii=True)
        tmp_path.write_text(text, encoding="utf-8")
        try:
            max_attempts = 5
            for attempt in range(1, max_attempts + 1):
                try:
                    tmp_path.replace(self.path)
                    break
                except PermissionError:
                    if attempt < max_attempts:
                        time.sleep(0.5)
                    else:
                        self.path.write_text(text, encoding="utf-8")
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    # -- Public API --

    def initialize(self, pattern: str, nodes: List[Dict], cycles: List[Dict]) -> None:
        """Set up tracking for a new run."""
        with self._lock:
            self.data["state"] = "running"
            self.data["pattern"] = pattern
            self.data["activity"] = "Starting execution"

            # Initialize node states
            for node in nodes:
                self.data["nodes"][node["name"]] = {
                    "state": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "iteration": None,
                    "parallel_group": node.get("parallel_group"),
                    "model": self._resolve_node_model(node),
                    "tokens": {"input": 0, "output": 0},
                }

            # Initialize cycle states
            for cycle in cycles:
                if cycle["type"] == "self-loop":
                    key = cycle["agent"]
                elif cycle["type"] == "bipartite":
                    key = f"{cycle['producer']}-{cycle['evaluator']}"
                else:
                    key = cycle.get("agent", "unknown")
                self.data["cycles"][key] = {
                    "type": cycle["type"],
                    "current_round": 0,
                    "max_rounds": cycle.get("max_iterations", cycle.get("max_rounds", 3)),
                    "state": "pending",
                }

            self._append_event("INFO", f"Run initialized: pattern={pattern}, {len(nodes)} nodes, {len(cycles)} cycles")
            self._write_locked()

    def add_nodes(self, nodes: List[Dict]) -> None:
        """Register nodes added after the run has already started."""
        if not nodes:
            return

        with self._lock:
            added = 0
            for node in nodes:
                name = node["name"]
                if name in self.data["nodes"]:
                    continue
                self.data["nodes"][name] = {
                    "state": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "iteration": None,
                    "parallel_group": node.get("parallel_group"),
                    "model": self._resolve_node_model(node),
                    "tokens": {"input": 0, "output": 0},
                }
                added += 1

            if added:
                self._append_event("INFO", f"Added {added} dynamic node(s)")
                self._write_locked()

    def _resolve_node_model(self, node: Dict[str, Any]) -> str:
        """Resolve a simple display label for a node's configured model."""
        agent_file = node.get("agent_file")
        if not agent_file:
            return "Unknown"

        for candidate in agent_path_candidates(self.run_dir, agent_file):
            raw_model = read_agent_frontmatter(candidate).get("model")
            if raw_model:
                return normalize_model_label(raw_model)
        return "Unknown"

    def set_node_state(
        self,
        name: str,
        state: str,
        iteration: Optional[int] = None,
    ) -> None:
        """Update a node's state."""
        with self._lock:
            node = self.data["nodes"].get(name)
            if node is None:
                return
            node["state"] = state
            if iteration is not None:
                node["iteration"] = iteration
            ts = _now_iso()
            if state == "running" and not node["started_at"]:
                node["started_at"] = ts
            elif state in ("completed", "failed", "cancelled", "terminated"):
                node["completed_at"] = ts
            self._write_locked()

    def set_cycle_state(
        self,
        key: str,
        state: str,
        current_round: Optional[int] = None,
    ) -> None:
        """Update a cycle's state."""
        with self._lock:
            cycle = self.data["cycles"].get(key)
            if cycle is None:
                return
            cycle["state"] = state
            if current_round is not None:
                cycle["current_round"] = current_round
            self._write_locked()

    def set_activity(self, activity: str) -> None:
        with self._lock:
            self.data["activity"] = activity
            self._write_locked()

    def set_state(self, state: str, detail: Optional[str] = None) -> None:
        with self._lock:
            self.data["state"] = state
            if detail:
                self.data["activity"] = detail
            self._write_locked()

    def set_final_output(self, path: str) -> None:
        with self._lock:
            self.data["final_output"] = path
            self._write_locked()

    def add_error(self, message: str) -> None:
        with self._lock:
            errors = self.data["errors"]
            errors.append({"ts": _now_iso(), "message": message})
            if len(errors) > 50:
                del errors[:-50]
            self._append_event("ERROR", message)
            self._write_locked()

    def append_event(self, message: str, level: str = "INFO") -> None:
        with self._lock:
            self._append_event(level, message)
            self._write_locked()

    # -- Live token polling --

    def start_token_polling(self) -> None:
        """Start the background thread that reads active log files every 2s."""
        self._poller_thread = threading.Thread(
            target=self._poll_tokens_loop, daemon=True,
        )
        self._poller_thread.start()

    def stop_token_polling(self) -> None:
        """Signal the poller to stop and wait for it."""
        self._poller_stop.set()
        if self._poller_thread:
            self._poller_thread.join(timeout=5)

    def register_active_log(self, name: str, log_path: Path) -> None:
        """Register a log file for live token polling."""
        with self._lock:
            self._active_logs.setdefault(name, []).append(log_path)

    def unregister_active_logs(self, name: str) -> None:
        """Remove all registered log files for a node."""
        with self._lock:
            self._active_logs.pop(name, None)

    def _poll_tokens_loop(self) -> None:
        """Background loop: parse active logs, update token counts in status.

        The displayed total = cumulative (finalized rounds) + live (current logs).
        """
        while not self._poller_stop.wait(2.0):
            with self._lock:
                snapshot = {k: list(v) for k, v in self._active_logs.items()}
            if not snapshot:
                continue
            changed = False
            with self._lock:
                for name, log_paths in snapshot.items():
                    live_in = 0
                    live_out = 0
                    for lp in log_paths:
                        inp, out = self._parse_log_tokens(lp)
                        live_in += inp
                        live_out += out
                    node = self.data["nodes"].get(name)
                    if node is None:
                        continue
                    old = node.get("tokens", {})
                    cum_in = int(old.get("cumulative_input", 0) or 0)
                    cum_out = int(old.get("cumulative_output", 0) or 0)
                    display_in = cum_in + live_in
                    display_out = cum_out + live_out
                    if display_in != old.get("input", 0) or display_out != old.get("output", 0):
                        node["tokens"]["input"] = display_in
                        node["tokens"]["output"] = display_out
                        changed = True
                if changed:
                    self._write_locked()

    # -- Token parsing --

    def update_node_tokens(self, name: str, log_path: Path) -> None:
        """Final token update after agent completion.

        Parses the completed log file and adds its tokens to the node's
        cumulative total.  Previous rounds' tokens are preserved because
        we only *add* the delta from this log file rather than replacing
        the running total.  Also unregisters the log from live polling.
        """
        # Unregister from live polling (final parse is authoritative)
        with self._lock:
            paths = self._active_logs.get(name, [])
            if log_path in paths:
                paths.remove(log_path)

        # Parse this log file's tokens
        log_in, log_out = self._parse_log_tokens(log_path)

        with self._lock:
            node = self.data["nodes"].get(name)
            if node is None:
                return
            old = node.get("tokens", {})
            # Store cumulative total and record this log's contribution
            # so the live poller can compute the correct running total.
            prev_in = int(old.get("cumulative_input", 0) or 0)
            prev_out = int(old.get("cumulative_output", 0) or 0)
            cum_in = prev_in + log_in
            cum_out = prev_out + log_out
            node["tokens"] = {
                "input": cum_in,
                "output": cum_out,
                "cumulative_input": cum_in,
                "cumulative_output": cum_out,
            }
            self._write_locked()

    @staticmethod
    def _parse_log_tokens(log_path: Path) -> tuple:
        """Return (input_tokens, output_tokens) from a stream-json log.

        Reads both ``assistant`` events (available mid-execution) and the
        final ``result`` event.

        For the live display, cache read/write tokens are intentionally
        excluded so the counter tracks Claude Code's visible input/output
        usage rather than cache bookkeeping. Assistant messages are also
        deduplicated by message ID, since stream-json can emit multiple
        records for the same message.
        """
        assistant_in = 0
        assistant_out = 0
        result_in = 0
        result_out = 0
        seen_assistant_ids = set()
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or '"usage"' not in stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    etype = obj.get("type")
                    if etype == "assistant":
                        message = obj.get("message", {})
                        message_id = message.get("id")
                        if message_id in seen_assistant_ids:
                            continue
                        if message_id:
                            seen_assistant_ids.add(message_id)
                        usage = message.get("usage", {})
                        assistant_in += usage.get("input_tokens", 0)
                        assistant_out += usage.get("output_tokens", 0)
                    elif etype == "result":
                        usage = obj.get("usage", {})
                        result_in = usage.get("input_tokens", 0)
                        result_out = usage.get("output_tokens", 0)
        except OSError:
            pass
        total_in = result_in if result_in > 0 else assistant_in
        total_out = result_out if result_out > 0 else assistant_out
        return total_in, total_out

    def _append_event(self, level: str, message: str) -> None:
        events = self.data["events"]
        events.append({
            "ts": _now_iso(),
            "level": level.upper(),
            "message": message,
        })
        if len(events) > 250:
            del events[:-250]
