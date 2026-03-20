---
name: multi-agent-graph-compose
description: "Guides the user through selecting and configuring a multi-agent execution pattern for their task. Triggers when the user invokes /multi-agent-graph:run."
---

# Multi-Agent Graph: Compose

This skill instructs Claude on how to guide the user through setting up a multi-agent execution pattern for their task.

## Phase 0: Load Plugin Configuration

Before asking the user anything, check for saved settings. The plugin data directory is derived from the plugin ID: `~/.claude/plugins/data/claude-multi-agent-cingulate1-marketplace/`.

1. Try to read `~/.claude/plugins/data/claude-multi-agent-cingulate1-marketplace/config.json`. Store this path as `{data_dir}/config.json`.
2. If the file exists and contains a `run_directory` value and `prompt_for_directory` is `false`: store the run directory for use in Phase 2. You will not need to ask the user about it.
3. Otherwise (file missing, `prompt_for_directory` is `true`, or key absent): you will ask the user during Phase 1.

## Phase 1: Socratic Dialogue

All user-facing questions happen in this phase. By the end of Phase 1, Claude should know the exact graph — every node, every edge, every model assignment — but nothing has been scaffolded or generated yet.

### Understand the Task

Ask the user what they want accomplished. Listen for:
- Single clear answer vs. multiple valid perspectives
- Subjective quality vs. objectively verifiable output
- Whether the work can be parallelized
- Whether iterative refinement is needed

If the user already described their task when invoking the command, move directly to resolving the run directory (or pattern recommendation if the directory is already configured).

### Resolve Run Directory

If Phase 0 already resolved a default run directory, skip this step.

Otherwise, ask the user:

> Where should I put the run directory for this workflow? This directory will contain agent outputs, logs, and scoring artifacts. Example: `C:/temp/multi-agent-runs`

After the user provides a directory:
- Ask if they want to save this as their default for future runs (skipping this question next time).
- If yes, write `~/.claude/plugins/data/claude-multi-agent-cingulate1-marketplace/config.json`:
  ```json
  {
    "run_directory": "<user's choice>",
    "prompt_for_directory": false
  }
  ```
- If no, use the directory for this run only without saving.

Store the resolved directory path as `{run_directory}` for use in Phase 2.

### The Seven Patterns

#### Iterative Refinement Patterns

These patterns refine a single artifact through repeated cycles.

**Chained Iteration** — One agent refines its own output in a self-loop. Best when success is objectively self-checkable against a concrete constraint (word count, format compliance, coverage of specific items). The agent checks its own work and iterates until the constraint is met or the limit is reached. Not for subjective quality, source verification, or tasks where multiple perspectives matter. Topology: 1 agent, self-loop (default max 3 iterations).

**RAG-Grounded Refinement** — A generator produces an artifact while a separate evaluator checks it against authoritative sources. The evaluator has access to source material that the generator may or may not see (hidden-methodology setups are supported). Best when factual fidelity or methodological compliance matters and a clear source corpus exists. Topology: 2 agents, bipartite cycle (default max 5 rounds).

**Rubric-Based Refinement** — A generator produces an artifact while a separate evaluator scores it on multiple quality dimensions using a rubric. The rubric can be user-specified or evaluator-generated. The evaluator gives per-dimension scores and actionable feedback without revealing the rubric criteria to the generator. Best when quality is subjective and multi-dimensional — writing quality, analytical depth, persuasiveness. Not for single-property optimization (use Chained Iteration instead). Topology: 2 agents, bipartite cycle with an evaluator-init phase (default max 5 rounds).

#### Panel Patterns

These patterns use multiple agents with distinct perspectives working on the same question.

**Consensus Panel** — N panelists independently address a question, then refine after reading each other's work, then a synthesizer extracts what they agree on. Disagreement is filtered out — the output is the common ground. Best when you want reliable, broadly-endorsed conclusions. Topology: 2N+1 agents (N initial + N refinement + 1 synthesizer), three-phase pipeline.

**Debate Panel** — N panelists independently answer a question, then engage in R rounds of adversarial debate where they critique each other's reasoning and may switch positions. A two-stage scoring pipeline selects the winner: Haiku evaluates semantic position changes and convergence across rounds, then a deterministic algorithm computes final scores from those evaluations. Panelists are never told how the winner is selected. Best when you want the answer that survives structured challenge — choosing between strategies, making a contested judgment call, stress-testing a recommendation. Works best on tasks with discrete candidate answers. Topology: N*(R+1)+2 nodes (N panelists across R+1 phases + 2 scorer scripts), acyclic.

**Dissensus Integration** — N panelists independently analyze from different lenses, then an integrator weaves their unique contributions into a unified artifact. Unlike Consensus, this preserves tension and harvests what each perspective uniquely sees rather than filtering for agreement. Best when complementary coverage matters more than convergence. Topology: N+1 agents (N panelists + 1 integrator), two-phase pipeline.

#### Decomposition Pattern

**Parallel Decomposition** — A decomposer partitions the task into independent assignments, then workers execute them in parallel. Worker outputs are collected, not synthesized. Best when the task naturally splits into pieces that don't need reconciliation — batch processing, independent analyses, coverage across categories. Topology: 1 decomposer + N workers (fixed or dynamic count), fan-out.

### Custom or Hybrid Graphs

If the user asks for a variation on a built-in pattern, a combination of patterns, or describes a custom topology (including Mermaid diagrams), read `${CLAUDE_PLUGIN_ROOT}/skills/compose/references/custom-graph.md` and follow its instructions instead of the standard pattern selection flow below. The custom graph reference covers topology elicitation, node specification, validation, and prompt generation for arbitrary graphs.

### Recommend a Pattern

Use the pattern descriptions above to match the user's task. Key differentiators:

| If the task needs... | Use |
|---------------------|-----|
| Hitting a specific, checkable constraint | Chained Iteration |
| Factual accuracy against a source corpus | RAG-Grounded Refinement |
| Subjective multi-dimensional quality | Rubric-Based Refinement |
| Common ground across perspectives | Consensus Panel |
| The answer that survives adversarial challenge | Debate Panel |
| Comprehensive coverage from complementary lenses | Dissensus Integration |
| Throughput on independent subtasks | Parallel Decomposition |

Explain WHY the recommended pattern fits. Mention 1-2 alternatives and why they're less suited. Let the user confirm or choose differently.

### Configure the Pattern

Once a pattern is selected, read its reference to get the pattern-specific questions:

#### Reference Load Map

- Chained Iteration → `${CLAUDE_PLUGIN_ROOT}/skills/compose/references/chained-iteration.md`
- RAG-Grounded Refinement → `${CLAUDE_PLUGIN_ROOT}/skills/compose/references/rag-grounded-refinement.md`
- Rubric-Based Refinement → `${CLAUDE_PLUGIN_ROOT}/skills/compose/references/rubric-based-refinement.md`
- Consensus Panel → `${CLAUDE_PLUGIN_ROOT}/skills/compose/references/consensus-panel.md`
- Debate Panel → `${CLAUDE_PLUGIN_ROOT}/skills/compose/references/debate-panel.md`
- Dissensus Integration → `${CLAUDE_PLUGIN_ROOT}/skills/compose/references/dissensus-integration.md`
- Parallel Decomposition → `${CLAUDE_PLUGIN_ROOT}/skills/compose/references/parallel-decomposition.md`
- Custom / Hybrid Graph → `${CLAUDE_PLUGIN_ROOT}/skills/compose/references/custom-graph.md`

Read only the selected pattern's reference. If choosing between two close options, you may read both candidates.

Ask the questions from the reference's **Ask the User** section. Be conversational — suggest defaults where the reference provides them. Do not improvise pattern-specific rules; follow the reference.

### Select Models

Default model assignments:
- **Opus**: All reasoning-heavy agents (panelists, evaluators, synthesizers, generators, integrators, selectors)
- **Sonnet**: Mechanical/structured agents (workers in Parallel Decomposition)

Present the proposed model assignment for each agent in the graph. Ask the user if they want to override any.

### Confirm Full Configuration

Summarize everything decided so far:
- Task description
- Pattern and why it was chosen
- Agent count, names, and topology (which agents exist, what depends on what, any cycles)
- Pattern-specific settings (from the reference's **Ask the User** answers)
- Model assignment per agent
- Tool assignment per agent (from the reference's **Tool Assignments** section; note any additions needed for the task, e.g. WebSearch for web research or Bash for code execution)

Ask the user to confirm. If they want changes, go back to the relevant step. Once confirmed, proceed to Phase 2.

## Phase 2: Generation

The user has confirmed the full configuration. No more questions — execute the following steps. Re-read the active pattern reference before generating.

### Generate Run Directory

Create the run directory using the `{run_directory}` resolved in Phase 1 (or Phase 0):
```
{run_directory}/{YYMMDD}_{slug}/
  output/
  agents/
  logs/
```

Where `{slug}` is a short kebab-case summary of the task (max 40 chars).

### Generate Agent Files

For each agent required by the pattern, run:

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/compose/scripts/create-subagent.py" <name> <description> <tools> <model> <output_dir>
```

- `name` — the agent's identifier (used as the filename: `{name}.md`)
- `description` — what the agent does
- `tools` — comma-delimited list (e.g., `"Read,Write,WebSearch,WebFetch"`)
- `model` — one of: `haiku`, `sonnet`, `opus`
- `output_dir` — the run directory's agents folder: `{run_dir}/agents`

### Write Agent Prompts

For each agent, write a prompt file to `{run_dir}/agents/{agent-name}-prompt.txt`. The active pattern reference contains a prompt template for each subagent type — follow the template, filling in the task-specific slots marked with `{CURLY_BRACES}`.

These prompt files are passed to the agent via `-p` when the orchestrator invokes it. They are the agent's only source of task context — the agent has no other conversation history.

After writing all prompts, fill in the body of each agent's `.md` file:
- Replace `{NAME}` with the agent's display name
- Replace `{PLACEHOLDER_PERSONA}` with a brief persona reinforcing the agent's role from the prompt

### Generate execution_plan.json

Write the execution plan to `{run_dir}/execution_plan.json`:

```json
{
  "pattern": "{pattern-name}",
  "run_dir": "{absolute_path_to_run_dir}",
  "plugin_dir": "{absolute_path_to_plugin}",
  "nodes": [...],
  "cycles": [...],
  "final_output": "output/{final-output-filename}.md"
}
```

Node format:
```json
{
  "name": "{agent-name}",
  "agent_file": "{agent-name}.md",
  "depends_on": ["dep1", "dep2"],
  "parallel_group": "group-name-or-null",
  "outputs": ["output/filename.md"]
}
```

Cycle format (self-loop):
```json
{
  "type": "self-loop",
  "agent": "{agent-name}",
  "max_iterations": 3,
  "exit_signal_file": "output/constraint-met.flag"
}
```

Cycle format (bipartite):
```json
{
  "type": "bipartite",
  "producer": "{producer-name}",
  "evaluator": "{evaluator-name}",
  "max_rounds": 5,
  "exit_signal_file": "output/evaluation-pass.flag"
}
```

### Validate Prompts

After writing all prompts and the execution plan, run the prompt validator:

```bash
python "{PLUGIN_ROOT}/scripts/validate_prompts.py" --plan "{run_dir}/execution_plan.json"
```

This checks that each agent's prompt file contains the absolute output path from the execution plan in its final lines. If any prompt fails validation, rewrite its last line to include a clear, direct instruction specifying the exact file path to write to — e.g., `Write your output to D:\path\to\output\agent-name.md`

Do not proceed to launch until all prompts pass validation.

### Launch Orchestrator

After writing all files, launch the orchestrator as a background process:

```bash
python "{PLUGIN_ROOT}/scripts/orchestrator.py" --plan "{run_dir}/execution_plan.json" --geometry 900x700+4740+904
```

Use `run_in_background: true` with the Bash tool. Then tell the user:
- The orchestrator is running
- The graph monitor GUI should have opened
- The run directory path
- That you'll report the results when execution completes

Do not launch workflow agents with the `Agent` tool. The orchestrator invokes them via CLI with per-agent tool restrictions.

### Monitor and Report

The orchestrator launches a sidecar process (`run_monitor.py`) that polls agent logs every 3 seconds and maintains two files in `{run_dir}/logs/`:

- **`run-status.md`** — a compact markdown table (~350 tokens for 8 agents) showing each agent's state, token usage, compaction status, and completion. Overwritten every 3 seconds.
- **`timeline.jsonl`** — an append-only sanitized event log capturing every tool use, message, and compaction event with cumulative token counts. This is the clean, structured version of the raw agent logs — never read the raw `.log` files directly.

After launching, set up a recurring 90-second health check. Use a Bash command with `sleep 90` to:

1. **Read `{run_dir}/logs/run-status.md`** for the full snapshot. Report any agents showing `Compacted: YES` — this means the agent's context window was exceeded and its output quality is likely compromised. The user may want to kill and re-run.
2. **Tail `{run_dir}/logs/timeline.jsonl`** (last ~50 lines) to check prompt adherence — are agents reading the right files? Are they progressing or stalled?

```bash
sleep 90 && cat "{run_dir}/logs/run-status.md" && echo "---" && tail -50 "{run_dir}/logs/timeline.jsonl"
```

Run this as a background Bash command with `run_in_background: true`. When the check completes, review the results and launch another 90-second cycle if agents are still running. Report compaction warnings to the user immediately.

**Completion.** Also check `{run_dir}/logs/status.json` on each cycle. When the `state` field becomes `"completed"` or `"failed"`:

- Read the final output file (from `final_output` in the execution plan)
- Present a concise summary to the user
- If failed, read the errors and report what went wrong
- Tell the user the full output is in the run directory
- Ask if they want to keep the agent definitions (copy to `~/.claude/agents/`) or let them be cleaned up with the run
