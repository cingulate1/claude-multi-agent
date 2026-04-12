#!/usr/bin/env python3
"""Orchestrator for multi-agent-graph execution runs.

Reads an execution_plan.json and runs agents through the directed graph,
handling parallel groups, self-loops, and bipartite cycles natively.

Usage:
    python orchestrator.py --plan <run_dir>/execution_plan.json
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from shared import read_agent_frontmatter, resolve_agent_path
from status_tracking import RunStatusTracker
from validate_prompts import validate_all

PLUGIN_ROOT = Path(__file__).parent.parent
AGENT_TIMEOUT = 1800  # 30 minutes per agent invocation


def _plugin_name() -> str:
    """Read the plugin name from .claude-plugin/plugin.json."""
    manifest = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return data["name"]
    except (OSError, json.JSONDecodeError, KeyError):
        return "multi-agent-graph"


PLUGIN_NAME = _plugin_name()


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def _agent_env():
    """Return an environment dict safe for spawning nested claude processes."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


# ---------------------------------------------------------------------------
# Agent staging
# ---------------------------------------------------------------------------

def stage_agent_file(src: Path) -> Path:
    """Copy one agent markdown file into the plugin agents/ dir."""
    target_dir = PLUGIN_ROOT / "agents"
    dst = target_dir / src.name
    shutil.copy2(src, dst)
    logging.info(f"  Staged: {src.name}")
    return dst


def stage_agents(run_dir: Path) -> list[Path]:
    """Copy agent .md files from run_dir/agents/ into plugin agents/ dir.

    Returns list of staged file paths for cleanup.
    """
    source_dir = run_dir / "agents"
    target_dir = PLUGIN_ROOT / "agents"
    staged = []

    if not source_dir.is_dir():
        logging.warning(f"No agents directory at {source_dir}")
        return staged

    for src in source_dir.glob("*.md"):
        staged.append(stage_agent_file(src))

    logging.info(f"Staged {len(staged)} agent(s)")
    return staged


def unstage_agents(staged_files: list[Path]) -> None:
    """Remove previously staged agent files from the plugin agents/ dir."""
    removed = 0
    for f in staged_files:
        try:
            if f.exists():
                f.unlink()
                removed += 1
        except OSError as e:
            logging.warning(f"  Failed to unstage {f.name}: {e}")
    logging.info(f"Unstaged {removed} agent(s)")


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------


def _normalize_tools_arg(raw_tools: str | None) -> str | None:
    """Normalize frontmatter `tools:` to Claude CLI `--tools` format."""
    if not raw_tools:
        return None
    tools = [tool.strip() for tool in raw_tools.split(",") if tool.strip()]
    if not tools:
        return None
    return ",".join(tools)


def _sanitize_node_name(raw: str, fallback: str) -> str:
    """Normalize an arbitrary assignment label into a stable node name."""
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", (raw or "").strip().lower())
    value = value.strip("-._")
    return value or fallback


def _rewrite_frontmatter_name(text: str, agent_name: str) -> str:
    """Force the frontmatter `name:` field to match the generated agent name."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text

    for i in range(1, len(lines)):
        stripped = lines[i].strip()
        if stripped == "---":
            break
        if stripped.lower().startswith("name:"):
            lines[i] = f"name: {agent_name}"
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return text


def _load_assignment_list(run_dir: Path, template: dict) -> tuple[Path, list[dict]]:
    """Load a decomposer-produced JSON assignment manifest."""
    manifest_file = template.get("manifest_file")
    if not manifest_file:
        raise RuntimeError("Dynamic template missing `manifest_file`")

    manifest_path = run_dir / manifest_file
    if not manifest_path.exists():
        raise RuntimeError(f"Dynamic assignment manifest not found: {manifest_file}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Failed to read dynamic assignment manifest '{manifest_file}': {e}") from e

    manifest_key = template.get("manifest_key", "workers")
    if isinstance(manifest, list):
        assignments = manifest
    elif isinstance(manifest, dict):
        assignments = manifest.get(manifest_key, [])
    else:
        assignments = []

    if not isinstance(assignments, list):
        raise RuntimeError(
            f"Dynamic assignment manifest '{manifest_file}' must contain a list at '{manifest_key}'"
        )

    return manifest_path, assignments


def _materialize_dynamic_agent(
    run_dir: Path,
    template_agent_path: Path,
    agent_name: str,
    assignment_id: str,
    assignments_file: str,
    output_file: str,
) -> Path:
    """Create a concrete agent markdown file from a worker template."""
    try:
        text = template_agent_path.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"Failed to read dynamic agent template '{template_agent_path.name}': {e}") from e

    replacements = {
        "{{AGENT_NAME}}": agent_name,
        "{{ASSIGNMENT_ID}}": assignment_id,
        "{{ASSIGNMENTS_FILE}}": assignments_file,
        "{{OUTPUT_FILE}}": output_file,
    }
    for needle, replacement in replacements.items():
        text = text.replace(needle, replacement)

    text = _rewrite_frontmatter_name(text, agent_name)

    generated_path = run_dir / "agents" / f"{agent_name}.md"
    generated_path.write_text(text, encoding="utf-8")
    return generated_path


def _expand_dynamic_templates_for_node(
    source_node: str,
    plan: dict,
    plan_path: Path,
    run_dir: Path,
    nodes: list[dict],
    nodes_by_name: dict[str, dict],
    status: RunStatusTracker,
    staged_files: list[Path],
    expanded_template_ids: set[str],
) -> None:
    """Materialize dynamic worker nodes after a source node completes."""
    templates = plan.get("dynamic_templates", [])
    if not templates:
        return

    existing_names = set(nodes_by_name)

    for index, template in enumerate(templates):
        template_id = template.get("id") or f"dynamic-template-{index + 1}"
        if template_id in expanded_template_ids:
            continue
        if template.get("after_node") != source_node:
            continue

        manifest_path, assignments = _load_assignment_list(run_dir, template)
        if not assignments:
            raise RuntimeError(
                f"Dynamic template '{template_id}' produced zero assignments in '{manifest_path.name}'"
            )

        max_dynamic_workers = int(template.get("max_dynamic_workers", 20))
        if len(assignments) > max_dynamic_workers:
            raise RuntimeError(
                f"Dynamic template '{template_id}' requested {len(assignments)} workers, "
                f"which exceeds the current cap of {max_dynamic_workers}"
            )

        template_agent_path = resolve_agent_path(
            run_dir,
            template.get("template_name", "worker-template"),
            template.get("agent_template_file"),
        )
        if template_agent_path is None:
            raise RuntimeError(
                f"Dynamic template '{template_id}' could not resolve agent template "
                f"'{template.get('agent_template_file')}'"
            )

        output_field = template.get("output_field", "output")
        name_field = template.get("name_field", "name")
        assignment_id_field = template.get("assignment_id_field", "assignment_id")
        name_prefix = template.get("name_prefix", "worker")

        created_nodes: list[dict] = []
        for assignment_index, assignment in enumerate(assignments, start=1):
            if not isinstance(assignment, dict):
                raise RuntimeError(
                    f"Dynamic template '{template_id}' has a non-object assignment at index {assignment_index}"
                )

            raw_assignment_id = (
                assignment.get(assignment_id_field)
                or assignment.get("id")
                or assignment.get(name_field)
                or f"{name_prefix}-{assignment_index}"
            )
            assignment_id = str(raw_assignment_id)

            raw_name = (
                assignment.get(name_field)
                or assignment.get("node_name")
                or assignment_id
            )
            node_name = _sanitize_node_name(str(raw_name), f"{name_prefix}-{assignment_index}")
            base_name = node_name
            dedupe_index = 2
            while node_name in existing_names:
                node_name = f"{base_name}-{dedupe_index}"
                dedupe_index += 1
            existing_names.add(node_name)

            output_file = (
                assignment.get(output_field)
                or assignment.get("output")
                or assignment.get("output_file")
                or assignment.get("output_path")
            )
            if not output_file:
                raise RuntimeError(
                    f"Dynamic template '{template_id}' assignment '{assignment_id}' is missing an output path"
                )

            output_path = run_dir / str(output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            generated_agent = _materialize_dynamic_agent(
                run_dir=run_dir,
                template_agent_path=template_agent_path,
                agent_name=node_name,
                assignment_id=assignment_id,
                assignments_file=str(template.get("manifest_file")),
                output_file=str(output_file),
            )
            staged_files.append(stage_agent_file(generated_agent))

            node = {
                "name": node_name,
                "agent_file": generated_agent.name,
                "depends_on": list(template.get("depends_on", [source_node])),
                "parallel_group": template.get("parallel_group"),
                "outputs": [str(output_file)],
            }
            nodes.append(node)
            nodes_by_name[node_name] = node
            created_nodes.append(node)

        if created_nodes:
            status.add_nodes(created_nodes)
            expanded_template_ids.add(template_id)
            template["expanded"] = True
            template["expanded_count"] = len(created_nodes)
            plan["nodes"] = nodes
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            status.append_event(
                f"Expanded {template_id}: {len(created_nodes)} worker node(s) from {manifest_path.name}"
            )


def _load_prompt(run_dir: Path, agent_name: str) -> str:
    """Load the prompt file for an agent, prefixed with the working directory.

    Every prompt starts with the absolute run_dir path so agents can
    construct correct absolute paths for tool calls like Write.
    """
    header = f"Working directory: {run_dir}\nAll relative paths in this prompt are relative to the working directory above.\n\n"

    prompt_file = run_dir / "agents" / f"{agent_name}-prompt.txt"
    if prompt_file.is_file():
        text = prompt_file.read_text(encoding="utf-8").strip()
        if text:
            return header + text
        logging.warning(f"  Prompt file for '{agent_name}' is empty")
    else:
        logging.warning(f"  No prompt file for '{agent_name}'")
    return header.strip()


def _build_agent_cmd(
    agent_name: str,
    run_dir: Path,
    agent_file: str | None = None,
    node: dict | None = None,
) -> list[str]:
    """Build the Claude CLI command for an agent invocation.

    When *node* carries ``"full_agent": true``, the ``--agent`` flag is
    omitted so the process runs as a full Claude Code CLI instance
    (unrestricted tools, ability to spawn its own subagents).  Model,
    effort, and tools are still configurable — first from the agent's
    frontmatter file (if one exists), then from inline fields on the
    *node* dict.
    """
    prompt = _load_prompt(run_dir, agent_name)
    full_agent = bool((node or {}).get("full_agent"))

    cmd = ["claude"]
    if not full_agent:
        cmd.extend(["--agent", f"{PLUGIN_NAME}:{agent_name}"])
        cmd.extend(["--plugin-dir", str(PLUGIN_ROOT)])
    cmd.extend([
        "--add-dir", str(run_dir),
        "-p", prompt,
        "--verbose",
        "--output-format", "stream-json",
        "--no-session-persistence",
    ])

    agent_path = resolve_agent_path(run_dir, agent_name, agent_file)
    frontmatter = read_agent_frontmatter(agent_path) if agent_path else {}
    node = node or {}

    model = frontmatter.get("model") or node.get("model")
    if model:
        cmd.extend(["--model", model])

    effort = frontmatter.get("effort") or node.get("effort")
    if effort and effort in ("low", "medium", "high", "max"):
        cmd.extend(["--effort", effort])

    tools_arg = _normalize_tools_arg(
        frontmatter.get("tools") or node.get("tools")
    )
    if tools_arg and not full_agent:
        cmd.extend(["--tools", tools_arg])

    return cmd

def _log_diagnostics(agent_name: str, returncode: int, log_path: Path) -> None:
    """Log post-exit diagnostics for an agent process."""
    try:
        log_size = log_path.stat().st_size
    except OSError:
        log_size = -1

    logging.info(
        f"  Agent '{agent_name}' exited: code={returncode}, "
        f"log_size={log_size} bytes"
    )

    if log_size == 0:
        logging.warning(f"  Agent '{agent_name}' produced a 0-byte log")
    elif log_size > 0:
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if '"type":"error"' in stripped or '"type":"system"' in stripped:
                        display = stripped[:500] + ("..." if len(stripped) > 500 else "")
                        logging.warning(f"  [{agent_name}] {display}")
        except OSError:
            pass


def run_agent(
    agent_name: str,
    run_dir: Path,
    log_path: Path,
    agent_file: str | None = None,
    timeout: int | None = None,
    node: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run a Claude agent synchronously. Returns the CompletedProcess."""
    cmd = _build_agent_cmd(agent_name, run_dir, agent_file, node=node)
    logging.info(f"  Running: {agent_name}")
    logging.debug(f"  cmd: {' '.join(cmd)}")

    with open(log_path, "w", encoding="utf-8") as f:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=f,
            stderr=subprocess.STDOUT,
            env=_agent_env(),
            cwd=str(run_dir),
            timeout=timeout or AGENT_TIMEOUT,
        )

    _log_diagnostics(agent_name, result.returncode, log_path)
    return result


def run_script(
    node_name: str,
    run_dir: Path,
    log_path: Path,
    script: str,
    script_args: list[str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """Run a script node directly (no LLM agent). Returns the CompletedProcess."""
    script_path = run_dir / script
    if not script_path.is_file():
        script_path = PLUGIN_ROOT / script
    if not script_path.is_file():
        raise RuntimeError(f"Script not found for node '{node_name}': {script}")

    cmd = [sys.executable, str(script_path)]
    if script_args:
        cmd.extend(script_args)
    logging.info(f"  Running script: {node_name} -> {script}")
    logging.debug(f"  cmd: {' '.join(cmd)}")

    with open(log_path, "w", encoding="utf-8") as f:
        result = subprocess.run(
            cmd,
            cwd=run_dir,
            stdin=subprocess.DEVNULL,
            stdout=f,
            stderr=subprocess.STDOUT,
            env=_agent_env(),
            timeout=timeout or AGENT_TIMEOUT,
        )

    logging.info(f"  Script '{node_name}' exited: code={result.returncode}")
    return result


def run_agents_parallel(agents: list[dict], run_dir: Path, log_dir: Path) -> dict[str, int]:
    """Run multiple agents in parallel. Returns dict mapping name -> returncode."""
    procs = {}
    log_handles = {}
    log_paths = {}

    for agent in agents:
        name = agent["name"]
        log_path = log_dir / f"{name}.log"
        cmd = _build_agent_cmd(name, run_dir, agent.get("agent_file"), node=agent)
        logging.info(f"  [parallel] {name}")
        fh = open(log_path, "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=fh,
                stderr=subprocess.STDOUT,
                env=_agent_env(),
                cwd=str(run_dir),
            )
        except Exception:
            fh.close()
            raise
        procs[name] = proc
        log_handles[name] = fh
        log_paths[name] = log_path

    results = {}
    for name, proc in procs.items():
        try:
            proc.wait(timeout=AGENT_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            logging.error(f"  Agent {name} timed out after {AGENT_TIMEOUT}s")
        finally:
            log_handles[name].close()
        results[name] = proc.returncode
        _log_diagnostics(name, proc.returncode, log_paths[name])

    return results


# ---------------------------------------------------------------------------
# Cycle execution
# ---------------------------------------------------------------------------

def _run_self_loop(
    cycle: dict,
    node_entry: dict,
    run_dir: Path,
    log_dir: Path,
    status: RunStatusTracker,
) -> bool:
    """Execute a self-loop cycle: one agent invoked repeatedly until exit signal or max iterations.

    Returns True if the cycle completed successfully, False if the agent failed.
    """
    agent_name = cycle["agent"]
    max_iter = cycle.get("max_iterations", 3)
    exit_signal = cycle.get("exit_signal_file")
    cycle_key = agent_name

    logging.info(f"  SELF-LOOP: {agent_name} (max {max_iter} iterations)")
    status.set_cycle_state(cycle_key, "running", current_round=0)

    for i in range(1, max_iter + 1):
        is_final = i == max_iter

        # Write final round signal if last iteration
        if is_final:
            signal_path = run_dir / "_final_round"
            signal_path.write_text(
                f"Iteration {i} of {max_iter}. This is the final iteration.",
                encoding="utf-8",
            )

        status.set_node_state(agent_name, "running", iteration=i)
        status.set_cycle_state(cycle_key, "running", current_round=i)
        status.set_activity(f"Self-loop: {agent_name} iteration {i}/{max_iter}")

        iter_log = log_dir / f"{agent_name}-iter{i}.log"
        status.register_active_log(agent_name, iter_log)
        result = run_agent(
            agent_name,
            run_dir,
            iter_log,
            agent_file=node_entry.get("agent_file"),
            node=node_entry,
        )
        if result.returncode != 0:
            status.update_node_tokens(agent_name, iter_log)
            status.unregister_active_logs(agent_name)
            status.set_node_state(agent_name, "failed")
            status.set_cycle_state(cycle_key, "failed")
            status.add_error(
                f"Self-loop agent '{agent_name}' exited with code "
                f"{result.returncode} on iteration {i}"
            )
            logging.warning(
                f"[WARNING] Self-loop agent '{agent_name}' exited with code "
                f"{result.returncode} on iteration {i}"
            )
            # Clean up signal file
            signal_path = run_dir / "_final_round"
            if signal_path.exists():
                signal_path.unlink()
            return False
        status.update_node_tokens(agent_name, iter_log)
        status.set_node_state(agent_name, "pending", iteration=i)

        # Check exit signal
        if exit_signal and (run_dir / exit_signal).exists():
            logging.info(f"  Exit signal found after iteration {i}")
            status.append_event(f"Self-loop {agent_name}: constraint met at iteration {i}")
            break

        if is_final:
            logging.info(f"  Max iterations ({max_iter}) reached")
            status.append_event(f"Self-loop {agent_name}: max iterations reached")

    # Clean up signal file
    signal_path = run_dir / "_final_round"
    if signal_path.exists():
        signal_path.unlink()

    status.unregister_active_logs(agent_name)
    status.set_node_state(agent_name, "completed")
    status.set_cycle_state(cycle_key, "completed", current_round=max_iter)
    return True


def _run_bipartite_cycle(
    cycle: dict,
    nodes_by_name: dict,
    run_dir: Path,
    log_dir: Path,
    status: RunStatusTracker,
) -> bool:
    """Execute a bipartite cycle: alternating producer and evaluator.

    Returns True if the cycle completed successfully, False if either agent failed.
    """
    producer_name = cycle["producer"]
    evaluator_name = cycle["evaluator"]
    max_rounds = cycle.get("max_rounds", 5)
    exit_signal = cycle.get("exit_signal_file")
    cycle_key = f"{producer_name}-{evaluator_name}"

    logging.info(f"  BIPARTITE CYCLE: {producer_name} <-> {evaluator_name} (max {max_rounds} rounds)")
    status.set_cycle_state(cycle_key, "running", current_round=0)

    for round_num in range(1, max_rounds + 1):
        is_final = round_num == max_rounds

        # Signal final round
        if is_final:
            signal_path = run_dir / "_final_round"
            signal_path.write_text(
                f"Round {round_num} of {max_rounds}. This is the final round.",
                encoding="utf-8",
            )

        # Run producer
        status.set_node_state(producer_name, "running", iteration=round_num)
        status.set_cycle_state(cycle_key, "running", current_round=round_num)
        status.set_activity(f"Cycle round {round_num}/{max_rounds}: {producer_name}")

        producer_log = log_dir / f"{producer_name}-r{round_num}.log"
        status.register_active_log(producer_name, producer_log)
        result = run_agent(
            producer_name,
            run_dir,
            producer_log,
            agent_file=nodes_by_name[producer_name].get("agent_file"),
            node=nodes_by_name[producer_name],
        )
        if result.returncode != 0:
            status.update_node_tokens(producer_name, producer_log)
            status.unregister_active_logs(producer_name)
            status.unregister_active_logs(evaluator_name)
            status.set_node_state(producer_name, "failed")
            status.set_node_state(evaluator_name, "cancelled")
            status.set_cycle_state(cycle_key, "failed")
            status.add_error(
                f"Cycle producer '{producer_name}' exited with code "
                f"{result.returncode} on round {round_num}"
            )
            logging.warning(
                f"[WARNING] Cycle producer '{producer_name}' exited with code "
                f"{result.returncode} on round {round_num}"
            )
            # Clean up signal file
            signal_path = run_dir / "_final_round"
            if signal_path.exists():
                signal_path.unlink()
            return False
        status.update_node_tokens(producer_name, producer_log)
        status.set_node_state(producer_name, "pending", iteration=round_num)

        # Run evaluator
        status.set_node_state(evaluator_name, "running", iteration=round_num)
        status.set_activity(f"Cycle round {round_num}/{max_rounds}: {evaluator_name}")

        evaluator_log = log_dir / f"{evaluator_name}-r{round_num}.log"
        status.register_active_log(evaluator_name, evaluator_log)
        result = run_agent(
            evaluator_name,
            run_dir,
            evaluator_log,
            agent_file=nodes_by_name[evaluator_name].get("agent_file"),
            node=nodes_by_name[evaluator_name],
        )
        if result.returncode != 0:
            status.update_node_tokens(evaluator_name, evaluator_log)
            status.unregister_active_logs(producer_name)
            status.unregister_active_logs(evaluator_name)
            status.set_node_state(evaluator_name, "failed")
            status.set_cycle_state(cycle_key, "failed")
            status.add_error(
                f"Cycle evaluator '{evaluator_name}' exited with code "
                f"{result.returncode} on round {round_num}"
            )
            logging.warning(
                f"[WARNING] Cycle evaluator '{evaluator_name}' exited with code "
                f"{result.returncode} on round {round_num}"
            )
            # Cycle is a unit — if it didn't complete, both nodes failed
            status.set_node_state(producer_name, "failed")
            # Clean up signal file
            signal_path = run_dir / "_final_round"
            if signal_path.exists():
                signal_path.unlink()
            return False
        status.update_node_tokens(evaluator_name, evaluator_log)
        status.set_node_state(evaluator_name, "pending", iteration=round_num)

        # Check exit signal
        if exit_signal and (run_dir / exit_signal).exists():
            logging.info(f"  Exit signal found after round {round_num}")
            status.append_event(
                f"Bipartite cycle {producer_name}<->{evaluator_name}: "
                f"converged at round {round_num}"
            )
            break

        if is_final:
            logging.info(f"  Max rounds ({max_rounds}) reached")
            status.append_event(
                f"Bipartite cycle {producer_name}<->{evaluator_name}: "
                f"max rounds reached"
            )

    # Clean up signal file
    signal_path = run_dir / "_final_round"
    if signal_path.exists():
        signal_path.unlink()

    status.unregister_active_logs(producer_name)
    status.unregister_active_logs(evaluator_name)
    status.set_node_state(producer_name, "completed")
    status.set_node_state(evaluator_name, "completed")
    status.set_cycle_state(cycle_key, "completed")
    return True


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify(title: str, message: str) -> None:
    """Fire a Windows toast notification."""
    safe_title = title.replace("'", "''")
    safe_message = message.replace("'", "''")
    ps_cmd = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
        "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        "$nodes = $template.GetElementsByTagName('text'); "
        f"$nodes.Item(0).AppendChild($template.CreateTextNode('{safe_title}')) > $null; "
        f"$nodes.Item(1).AppendChild($template.CreateTextNode('{safe_message}')) > $null; "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Claude Code').Show($toast)"
    )
    subprocess.Popen(
        ["powershell", "-Command", ps_cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Main execution engine
# ---------------------------------------------------------------------------

def execute(plan_path: Path, gui: bool = True, geometry: str = None) -> int:
    """Execute the full directed graph from an execution plan.

    Returns an exit code:
      0 = all nodes completed successfully
      1 = total failure (no nodes completed, or orchestrator-level error)
      2 = partial completion (some succeeded, some failed/cancelled)
    """
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)

    run_dir = Path(plan["run_dir"]).resolve()
    pattern = plan["pattern"]
    nodes = plan["nodes"]
    cycles = plan.get("cycles", [])
    final_output = plan.get("final_output")

    nodes_by_name = {n["name"]: n for n in nodes}
    log_dir = run_dir / "logs"
    log_dir.mkdir(exist_ok=True)

    # Validate prompts before launching any agents
    success, validation_errors = validate_all(plan_path)
    if not success:
        msg = "Prompt validation failed:\n"
        for err in validation_errors:
            msg += f"  {err}\n"
        msg += "\nMake sure the final line of each prompt follows the format: Write your output to <absolute_path>"
        print(msg, file=sys.stderr)
        return 1

    # Initialize status tracking
    status = RunStatusTracker(run_dir)
    status.initialize(pattern, nodes, cycles)

    # Stage agents into plugin directory
    staged = stage_agents(run_dir)

    # Launch graph monitor GUI
    monitor_script = PLUGIN_ROOT / "scripts" / "graph_monitor.py"
    monitor_proc = None
    if gui and monitor_script.exists():
        cmd = [sys.executable, str(monitor_script), str(run_dir)]
        if geometry:
            cmd += ["--geometry", geometry]
        monitor_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_agent_env(),
        )

    # Launch run_monitor sidecar
    sidecar_proc = None
    sidecar_script = PLUGIN_ROOT / "scripts" / "run_monitor.py"
    if sidecar_script.exists():
        sidecar_proc = subprocess.Popen(
            [sys.executable, str(sidecar_script), "--run-dir", str(run_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # Start live token polling
    status.start_token_polling()

    exit_code = 0
    try:
        completed, failed = _execute_graph(
            plan,
            plan_path,
            nodes,
            cycles,
            nodes_by_name,
            run_dir,
            log_dir,
            status,
            staged,
        )

        if final_output:
            status.set_final_output(final_output)

        if not failed:
            status.set_state("completed", "All agents executed successfully")
            status.append_event("Run completed successfully")
            notify("multi-agent-graph Complete", f"Pattern: {pattern}")
            exit_code = 0
        elif not completed:
            status.set_state("failed", "All nodes failed")
            status.append_event("Run failed: no nodes completed successfully")
            notify("multi-agent-graph Failed", "All nodes failed")
            exit_code = 1
        else:
            summary = f"{len(completed)} succeeded, {len(failed)} failed/cancelled"
            status.set_state("completed", f"Partial completion: {summary}")
            status.append_event(f"Run partially completed: {summary}")
            notify("multi-agent-graph Partial", summary)
            exit_code = 2

    except Exception as e:
        status.add_error(str(e))
        status.set_state("failed", str(e))
        notify("multi-agent-graph Failed", str(e)[:100])
        exit_code = 1

    finally:
        status.stop_token_polling()
        unstage_agents(staged)
        # Signal GUI to save final render, then clean up child processes
        if monitor_proc and monitor_proc.poll() is None:
            signal_file = run_dir / "logs" / "_save_and_exit"
            signal_file.write_text("1")
            try:
                monitor_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                monitor_proc.terminate()
        if sidecar_proc and sidecar_proc.poll() is None:
            sidecar_proc.terminate()

    return exit_code


def _find_dependents(node_name: str, nodes: list[dict]) -> list[str]:
    """Find all nodes that directly or transitively depend on a given node."""
    # Build direct dependents map
    direct_deps: dict[str, list[str]] = {}
    for n in nodes:
        for dep in n.get("depends_on", []):
            direct_deps.setdefault(dep, []).append(n["name"])

    # BFS to find all transitive dependents
    dependents = []
    visited = set()
    queue = deque([node_name])
    while queue:
        current = queue.popleft()
        for child in direct_deps.get(current, []):
            if child not in visited:
                visited.add(child)
                dependents.append(child)
                queue.append(child)
    return dependents


def _cancel_dependents(
    failed_name: str,
    nodes: list[dict],
    completed: set,
    failed_set: set,
    status: RunStatusTracker,
) -> list[str]:
    """Cancel all unfinished nodes that depend (directly or transitively) on a failed node.

    Returns the list of newly cancelled node names.
    """
    dependents = _find_dependents(failed_name, nodes)
    cancelled = []
    for dep_name in dependents:
        if dep_name not in completed and dep_name not in failed_set:
            status.set_node_state(dep_name, "cancelled")
            failed_set.add(dep_name)
            cancelled.append(dep_name)
    return cancelled


def _execute_graph(
    plan: dict,
    plan_path: Path,
    nodes: list[dict],
    cycles: list[dict],
    nodes_by_name: dict,
    run_dir: Path,
    log_dir: Path,
    status: RunStatusTracker,
    staged_files: list[Path],
) -> tuple[set, set]:
    """Core graph execution loop.

    Returns (completed_set, failed_set) where:
      - completed_set contains names of nodes that succeeded
      - failed_set contains names of nodes that failed or were cancelled
    """
    completed = set()
    failed_set = set()  # nodes that failed, were terminated, or were cancelled
    expanded_template_ids: set[str] = set()

    # Build set of all node names that participate in cycles
    cycle_members = set()
    for cycle in cycles:
        if cycle["type"] == "self-loop":
            cycle_members.add(cycle["agent"])
        elif cycle["type"] == "bipartite":
            cycle_members.add(cycle["producer"])
            cycle_members.add(cycle["evaluator"])

    # Map cycle members to their cycle definition for quick lookup
    cycle_for_node = {}
    for cycle in cycles:
        if cycle["type"] == "self-loop":
            cycle_for_node[cycle["agent"]] = cycle
        elif cycle["type"] == "bipartite":
            cycle_for_node[cycle["producer"]] = cycle
            cycle_for_node[cycle["evaluator"]] = cycle

    # Track which cycles have been executed (by cycle key)
    executed_cycles = set()

    # The set of all "resolved" nodes (completed + failed + cancelled)
    resolved = set()

    while len(resolved) < len(nodes):
        # Find nodes whose dependencies are all resolved (completed or cancelled)
        # A node is "ready" if all its deps are completed (not just resolved —
        # if a dep failed/cancelled, the node itself should be cancelled).
        ready = []
        newly_blocked = []
        for n in nodes:
            name = n["name"]
            if name in resolved:
                continue
            deps = n.get("depends_on", [])
            # Check if any dependency failed/cancelled
            if any(dep in failed_set for dep in deps):
                newly_blocked.append(n)
                continue
            # Check if all dependencies completed
            if all(dep in completed for dep in deps):
                ready.append(n)

        # Cancel any nodes blocked by failed dependencies
        for blocked_node in newly_blocked:
            bname = blocked_node["name"]
            if bname not in failed_set:
                status.set_node_state(bname, "cancelled")
                failed_set.add(bname)
                resolved.add(bname)
                logging.info(f"  Cancelled '{bname}' (dependency failed)")
                status.append_event(f"Cancelled: {bname} (dependency failed)")

        if not ready:
            # Check if we're genuinely stuck or just done
            remaining = [n["name"] for n in nodes if n["name"] not in resolved]
            if remaining:
                # All remaining nodes have unresolved deps — cancel them
                for n in nodes:
                    if n["name"] not in resolved:
                        status.set_node_state(n["name"], "cancelled")
                        failed_set.add(n["name"])
                        resolved.add(n["name"])
                logging.warning(
                    f"[WARNING] Remaining nodes cancelled due to unresolvable dependencies: {remaining}"
                )
            break

        # Separate into cycle entries and non-cycle nodes
        ready_cycles = []
        ready_normal = []
        for node in ready:
            if node["name"] in cycle_members:
                cycle = cycle_for_node[node["name"]]
                # Determine cycle key
                if cycle["type"] == "self-loop":
                    key = cycle["agent"]
                else:
                    key = f"{cycle['producer']}-{cycle['evaluator']}"
                if key not in executed_cycles:
                    ready_cycles.append((key, cycle))
                    executed_cycles.add(key)
                # Skip — this node is handled by its cycle
            else:
                ready_normal.append(node)

        # Execute ready cycles
        for cycle_key, cycle in ready_cycles:
            if cycle["type"] == "self-loop":
                agent_name = cycle["agent"]
                node_entry = nodes_by_name[agent_name]
                success = _run_self_loop(cycle, node_entry, run_dir, log_dir, status)
                if success:
                    completed.add(agent_name)
                    resolved.add(agent_name)
                else:
                    failed_set.add(agent_name)
                    resolved.add(agent_name)
                    cancelled = _cancel_dependents(agent_name, nodes, completed, failed_set, status)
                    resolved.update(cancelled)
                    if cancelled:
                        logging.warning(
                            f"[WARNING] Agent '{agent_name}' exited with failure; "
                            f"dependent nodes {cancelled} cancelled"
                        )
                    else:
                        logging.warning(
                            f"[WARNING] Agent '{agent_name}' exited with failure; "
                            f"no dependents affected"
                        )

            elif cycle["type"] == "bipartite":
                producer = cycle["producer"]
                evaluator = cycle["evaluator"]
                success = _run_bipartite_cycle(cycle, nodes_by_name, run_dir, log_dir, status)
                if success:
                    completed.add(producer)
                    completed.add(evaluator)
                    resolved.add(producer)
                    resolved.add(evaluator)
                else:
                    # At least one of them failed — both are in failed_set
                    # (the cycle function already set their states)
                    failed_set.add(producer)
                    failed_set.add(evaluator)
                    resolved.add(producer)
                    resolved.add(evaluator)
                    # Cancel dependents of both
                    all_cancelled = []
                    for failed_name in (producer, evaluator):
                        cancelled = _cancel_dependents(failed_name, nodes, completed, failed_set, status)
                        resolved.update(cancelled)
                        all_cancelled.extend(cancelled)
                    if all_cancelled:
                        logging.warning(
                            f"[WARNING] Bipartite cycle {producer}<->{evaluator} failed; "
                            f"dependent nodes {all_cancelled} cancelled"
                        )

        # Group non-cycle ready nodes by parallel_group
        groups: dict[str | None, list[dict]] = {}
        for node in ready_normal:
            group = node.get("parallel_group")
            groups.setdefault(group, []).append(node)

        for group_name, group_nodes in groups.items():
            if len(group_nodes) == 1:
                # Single node — run sequentially
                node = group_nodes[0]
                name = node["name"]
                status.set_node_state(name, "running")
                status.set_activity(f"Running: {name}")

                agent_log = log_dir / f"{name}.log"
                status.register_active_log(name, agent_log)
                if node.get("node_type") == "script":
                    result = run_script(
                        name,
                        run_dir,
                        agent_log,
                        node["script"],
                        script_args=node.get("script_args"),
                    )
                else:
                    result = run_agent(
                        name,
                        run_dir,
                        agent_log,
                        agent_file=node.get("agent_file"),
                        node=node,
                    )

                if result.returncode != 0:
                    if node.get("node_type") != "script":
                        status.update_node_tokens(name, agent_log)
                    status.unregister_active_logs(name)
                    status.set_node_state(name, "failed")
                    failed_set.add(name)
                    resolved.add(name)
                    status.add_error(f"Node '{name}' exited with code {result.returncode}")
                    # Cancel dependents
                    cancelled = _cancel_dependents(name, nodes, completed, failed_set, status)
                    resolved.update(cancelled)
                    if cancelled:
                        logging.warning(
                            f"[WARNING] Agent '{name}' exited with code {result.returncode}; "
                            f"dependent nodes {cancelled} cancelled"
                        )
                        status.append_event(
                            f"Failed: {name} (code {result.returncode}); cancelled {cancelled}"
                        )
                    else:
                        logging.warning(
                            f"[WARNING] Agent '{name}' exited with code {result.returncode}; "
                            f"no dependents affected"
                        )
                        status.append_event(f"Failed: {name} (code {result.returncode})")
                    continue

                if node.get("node_type") != "script":
                    status.update_node_tokens(name, agent_log)
                status.unregister_active_logs(name)

                # Verify outputs
                for expected in node.get("outputs", []):
                    if not (run_dir / expected).exists():
                        logging.warning(f"Expected output missing: {expected}")

                status.set_node_state(name, "completed")
                completed.add(name)
                resolved.add(name)
                status.append_event(f"Completed: {name}")
                _expand_dynamic_templates_for_node(
                    source_node=name,
                    plan=plan,
                    plan_path=plan_path,
                    run_dir=run_dir,
                    nodes=nodes,
                    nodes_by_name=nodes_by_name,
                    status=status,
                    staged_files=staged_files,
                    expanded_template_ids=expanded_template_ids,
                )

            elif len(group_nodes) > 1:
                # Separate script nodes (deterministic) from agent nodes (LLM)
                agent_group = [n for n in group_nodes if n.get("node_type") != "script"]
                script_group = [n for n in group_nodes if n.get("node_type") == "script"]

                if agent_group:
                    names = [n["name"] for n in agent_group]
                    logging.info(f"Running in parallel: {names}")
                    status.set_activity(f"Running {len(names)} agents in parallel")
                    for n in agent_group:
                        status.set_node_state(n["name"], "running")
                        status.register_active_log(n["name"], log_dir / f"{n['name']}.log")

                    agents_list = [
                        {"name": n["name"], "agent_file": n.get("agent_file")}
                        for n in agent_group
                    ]
                    results = run_agents_parallel(agents_list, run_dir, log_dir)

                    for name, rc in results.items():
                        if rc != 0:
                            status.update_node_tokens(name, log_dir / f"{name}.log")
                            status.unregister_active_logs(name)
                            status.set_node_state(name, "failed")
                            failed_set.add(name)
                            resolved.add(name)
                            status.add_error(f"Agent '{name}' exited with code {rc}")
                            # Cancel dependents
                            cancelled = _cancel_dependents(name, nodes, completed, failed_set, status)
                            resolved.update(cancelled)
                            if cancelled:
                                logging.warning(
                                    f"[WARNING] Agent '{name}' exited with code {rc}; "
                                    f"dependent nodes {cancelled} cancelled"
                                )
                                status.append_event(
                                    f"Failed: {name} (code {rc}); cancelled {cancelled}"
                                )
                            else:
                                logging.warning(
                                    f"[WARNING] Agent '{name}' exited with code {rc}; "
                                    f"no dependents affected"
                                )
                                status.append_event(f"Failed: {name} (code {rc})")
                            continue

                        status.update_node_tokens(name, log_dir / f"{name}.log")
                        status.unregister_active_logs(name)
                        status.set_node_state(name, "completed")
                        completed.add(name)
                        resolved.add(name)
                        status.append_event(f"Completed: {name}")
                        _expand_dynamic_templates_for_node(
                            source_node=name,
                            plan=plan,
                            plan_path=plan_path,
                            run_dir=run_dir,
                            nodes=nodes,
                            nodes_by_name=nodes_by_name,
                            status=status,
                            staged_files=staged_files,
                            expanded_template_ids=expanded_template_ids,
                        )

                for node in script_group:
                    sname = node["name"]
                    status.set_node_state(sname, "running")
                    status.set_activity(f"Running script: {sname}")
                    script_log = log_dir / f"{sname}.log"
                    status.register_active_log(sname, script_log)
                    result = run_script(
                        sname, run_dir, script_log, node["script"],
                        script_args=node.get("script_args"),
                    )
                    if result.returncode != 0:
                        status.unregister_active_logs(sname)
                        status.set_node_state(sname, "failed")
                        failed_set.add(sname)
                        resolved.add(sname)
                        status.add_error(f"Script '{sname}' exited with code {result.returncode}")
                        cancelled = _cancel_dependents(sname, nodes, completed, failed_set, status)
                        resolved.update(cancelled)
                        if cancelled:
                            logging.warning(
                                f"[WARNING] Script '{sname}' exited with code {result.returncode}; "
                                f"dependent nodes {cancelled} cancelled"
                            )
                            status.append_event(
                                f"Failed: {sname} (code {result.returncode}); cancelled {cancelled}"
                            )
                        else:
                            logging.warning(
                                f"[WARNING] Script '{sname}' exited with code {result.returncode}; "
                                f"no dependents affected"
                            )
                            status.append_event(f"Failed: {sname} (code {result.returncode})")
                        continue

                    status.unregister_active_logs(sname)
                    status.set_node_state(sname, "completed")
                    completed.add(sname)
                    resolved.add(sname)
                    status.append_event(f"Completed: {sname}")
                    _expand_dynamic_templates_for_node(
                        source_node=sname,
                        plan=plan,
                        plan_path=plan_path,
                        run_dir=run_dir,
                        nodes=nodes,
                        nodes_by_name=nodes_by_name,
                        status=status,
                        staged_files=staged_files,
                        expanded_template_ids=expanded_template_ids,
                    )

    if failed_set:
        logging.info(f"Execution finished with failures: {sorted(failed_set)}")
    else:
        logging.info("All nodes completed")

    return completed, failed_set


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="multi-agent-graph orchestrator")
    parser.add_argument("--plan", required=True, help="Path to execution_plan.json")
    parser.add_argument("--no-gui", action="store_true", help="Suppress graph monitor GUI")
    parser.add_argument("--geometry", default=None, help="Window geometry for graph monitor (WxH+X+Y)")
    args = parser.parse_args()

    plan_path = Path(args.plan).resolve()
    run_dir = plan_path.parent

    log_dir = run_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    pipeline_log = log_dir / "orchestrator.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(pipeline_log, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    logging.info(f"Orchestrator started for: {run_dir}")
    logging.info(f"Plugin root: {PLUGIN_ROOT}")

    try:
        exit_code = execute(plan_path, gui=not args.no_gui, geometry=args.geometry)
        sys.exit(exit_code)
    except subprocess.TimeoutExpired as e:
        logging.error(f"Agent timed out: {e}")
        notify("multi-agent-graph Failed", "Agent timed out")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Unexpected failure: {e}")
        notify("multi-agent-graph Failed", str(e)[:100])
        sys.exit(1)


if __name__ == "__main__":
    main()
