# Custom Graph

## When This Applies

The user wants a topology that doesn't match any single built-in pattern. This includes:
- Combining two or more patterns (e.g., decomposition feeding into debates)
- Modifying a built-in pattern's structure (e.g., adding a synthesis node after a debate)
- Specifying a completely novel topology from scratch
- Providing a Mermaid diagram or similar graph notation

## Elicit the Topology

The user may describe their graph in many forms. Accept any of these and normalize to a node-edge structure:

**Mermaid diagram** — Parse the node IDs and directed edges. Ignore Mermaid subgraph groupings, styling, and edge labels — extract only the nodes and directed edges. If the user's edge labels represent conditions, ask whether these are sequential phases or true conditional branching (which the orchestrator does not support — all edges are unconditional). Ask the user what each node does; the diagram gives you structure but not semantics.

**Freeform description** — "I want three researchers to work independently, then a critic reviews all three, then the researchers revise based on the critique." Extract the implicit DAG: 3 parallel nodes → 1 critic node → 3 parallel revision nodes.

**Pattern combination** — "Decompose the task, then run a debate on each subtask." Map this to concrete patterns: a decomposer fans out to N debate subgraphs, each with their own panelists and scoring.

**Variation on a built-in** — "Like a consensus panel, but with two rounds of independent work before the synthesis." Read the relevant pattern reference first, then modify the topology as described.

After extracting the topology, confirm it back to the user as a node list with edges before proceeding. Include: node count, dependency structure, parallel groups, any cycles, and model assignments. Use a simple text format:

```
Nodes:
  1. researcher-a (parallel with 2, 3)
  2. researcher-b (parallel with 1, 3)
  3. researcher-c (parallel with 1, 2)
  4. critic (depends on 1, 2, 3)
  5. researcher-a-revision (depends on 4, parallel with 6, 7)
  6. researcher-b-revision (depends on 4, parallel with 5, 7)
  7. researcher-c-revision (depends on 4, parallel with 5, 6)
```

## Specify Each Node

For each node in the graph, you need seven pieces of information. Some the user will provide explicitly; others you should propose defaults and let the user override.

| Field | Ask or propose? | Notes |
|-------|----------------|-------|
| **Purpose** | Ask | What this node accomplishes. One sentence. |
| **Node type** | Propose | `agent` (default, LLM-powered), `full_agent` (unrestricted Claude Code CLI — see below), or `script` (Python script — for mechanical work like scoring, aggregation, formatting). |
| **Reads** | Derive from edges | The output files of its dependencies. |
| **Writes** | Propose | Default: `output/{node-name}.md`. The user rarely needs to override this. |
| **Persona** | Propose (agents only) | Derive from the node's purpose. A researcher node gets a researcher persona. |
| **Tools** | Propose (agents only) | Default: `Read,Write`. Add `WebSearch,WebFetch` if the task involves research, `Bash` if it involves code execution. |
| **Model** | Propose (agents only) | Default: opus for reasoning-heavy nodes, sonnet for mechanical/structured work. |
| **Effort** | Propose (agents only) | Optional. One of: `low`, `medium`, `high`, `max` (max is Opus-only). Omit to use session default. |

Ask about purpose for every node. Propose the rest in a summary table and let the user correct any row. This minimizes back-and-forth without sacrificing control.

### Full Agent Nodes

A **full agent** node runs as an unrestricted Claude Code CLI instance — no subagent sandbox, no tool restrictions, full access to spawn its own subagents. This is a specialized node type. Use it **only** when:

- The user explicitly requests it, OR
- The node's task requires invoking its own subagents to function correctly (e.g., an orchestrator-within-an-orchestrator, a node that delegates subtasks to child agents)

Full agent nodes do **not** have a subagent definition file (`.md`). Instead, model, effort, and tools are set directly on the execution plan node. They still receive a prompt file like any other agent node.

When proposing a node specification, **never default to `full_agent`** — always default to `agent`. Only suggest `full_agent` if the node's purpose clearly requires unrestricted CLI access, and explain why.

## Validate the Topology

Before generating, check these constraints. Catching violations here prevents failures at orchestration time.

**Acyclic or explicitly cycled.** Every cycle must be declared as a self-loop or bipartite cycle in the execution plan with a max iteration count and exit condition. If the user's graph has an implicit cycle (A → B → A), ask whether this is intentional iteration or a mistake. If intentional, ask for the exit condition and max iterations.

**Every node should have at least one output.** Nodes that don't write anything are invisible to downstream nodes — they can't pass information forward. The orchestrator does not enforce this as a hard constraint (it logs a warning but continues), but a node with no output is almost always a design error. If a node's purpose is purely evaluative (e.g., a critic that just scores), it still needs to write its assessment to a file.

**Parallel groups are truly independent.** Nodes in the same parallel group must not depend on each other. If the user puts two nodes in parallel but one reads the other's output, flag the contradiction.

**Script nodes vs. agent nodes.** If a node's work is mechanical (scoring, aggregation, formatting), propose making it a script node rather than an LLM agent. Script nodes are faster, cheaper, and deterministic. Note: script nodes within a parallel group run sequentially, even when agent nodes in the same group run in parallel.

---

## Generation (Phase 2 material below)

Everything above is Phase 1 — elicitation and validation. Everything below is Phase 2 — generating the artifacts. When re-reading this reference during Phase 2, start here.

## Generate Prompts for Custom Nodes

Built-in patterns have prompt templates. Custom graphs do not — you write each prompt from the node's purpose, reads, and writes. Follow this structure for every agent node:

Note: The final line below is validator-enforced — substitute only the path placeholder, preserve the rest verbatim. See SKILL.md "Mandatory Final Line" for the full rule.

```
You are {PERSONA_DESCRIPTION}.

## Task

{TASK_DESCRIPTION — derived from the node's purpose}

{CONTEXT_INSTRUCTION — what to read and why, derived from the node's dependencies}

{PROCEDURE — step-by-step if the task is complex, or a simple instruction if straightforward}

## Output

{OUTPUT_FORMAT — what the output should contain and how it should be structured}

Write your output to {ABSOLUTE_OUTPUT_PATH}
```

The prompt must be self-contained. The agent has no conversation history — this prompt is its entire world. Include everything it needs to do its job: what files to read, what to produce, where to write it.

For nodes that read multiple predecessors' outputs, list every file path explicitly. Do not use glob patterns or "read all files in output/" — the agent needs concrete paths.

**Prompt file naming:** Each prompt is saved to `{run_dir}/agents/{agent-name}-prompt.txt`. The orchestrator loads prompts by this convention and automatically prepends a `Working directory: {run_dir}` header — do not duplicate working directory instructions in the prompt body.

Script nodes do not get prompt files or agent files.

## Build the Execution Plan

The execution plan follows the same schema as built-in patterns.

### Agent nodes

```json
{
  "name": "researcher-a",
  "agent_file": "researcher-a.md",
  "depends_on": [],
  "parallel_group": "researchers",
  "outputs": ["output/researcher-a.md"]
}
```

The `agent_file` field references the `.md` file in the run's `agents/` directory. If omitted, the orchestrator falls back to `{name}.md`.

### Full agent nodes

Full agent nodes omit the `agent_file` and set `"full_agent": true`. Model, effort, and tools are specified inline on the node:

```json
{
  "name": "delegator",
  "full_agent": true,
  "depends_on": ["planner"],
  "parallel_group": null,
  "outputs": ["output/delegator.md"],
  "model": "opus",
  "effort": "high"
}
```

No `.md` subagent definition file is generated for full agent nodes. The orchestrator omits the `--agent` flag, so the process runs as a native Claude Code CLI instance with unrestricted tool access. The node still receives a prompt file (`{name}-prompt.txt`) like any other agent node.

### Script nodes

Script nodes have no agent file and no prompt file. They are Python scripts that the orchestrator runs directly:

```json
{
  "name": "aggregator",
  "node_type": "script",
  "script": "aggregate.py",
  "script_args": ["--format", "json"],
  "depends_on": ["researcher-a", "researcher-b"],
  "outputs": ["output/aggregated.md"]
}
```

The `script` path is resolved against the run directory first, then against the plugin root. `script_args` is an optional list of CLI arguments.

### Parallel groups

When all nodes in a group share the same dependencies, the orchestrator runs them as an atomic batch — all start together and the next phase waits for all to complete. Assign the same `parallel_group` string to nodes that should run concurrently. Nodes with `parallel_group: null` (or omitted) that become ready simultaneously will also run concurrently.

If a graph has nodes that could run concurrently but have different dependency sets, they may end up in different scheduling iterations. Explain any such behavior to the user when it affects expected parallelism.

### Cycles

Cycles are declared in the top-level `"cycles"` array of the execution plan, separate from the `"nodes"` array. Nodes that participate in cycles must still be declared in the `"nodes"` array — the cycle definition references them by name.

**Bipartite cycle** (producer-evaluator loop):

```json
{
  "type": "bipartite",
  "producer": "node-a",
  "evaluator": "node-b",
  "max_rounds": 5,
  "exit_signal_file": "output/exit-condition.flag"
}
```

The evaluator writes the exit flag file when the producer's output meets criteria.

**Self-loop** (single agent iterating on its own output):

```json
{
  "type": "self-loop",
  "agent": "refiner",
  "max_iterations": 3,
  "exit_signal_file": "output/constraint-met.flag"
}
```

The agent checks its own output against a constraint and writes the exit flag when satisfied.

**Final round signal:** On the last iteration of any cycle (self-loop or bipartite), the orchestrator writes a `_final_round` file to the run directory. Agent prompts for cycle nodes can instruct the agent to check for this file and produce their best final output if it exists.

### `final_output`

Set the `final_output` field in the execution plan to the output file of the last node in the DAG. This is the file the orchestrator reports as the run's result.

## Handle Pattern Combinations

When the user combines patterns, each sub-pattern retains its internal logic but connects to the others through explicit edges. The approach:

1. Identify the sub-patterns and their boundaries.
2. Read the reference for each sub-pattern involved.
3. Generate each sub-pattern's nodes using its reference's templates.
4. Add bridge nodes or edges that connect the sub-patterns.
5. Merge into a single flat execution plan — the orchestrator has no concept of nested sub-graphs.
6. Each sub-pattern's cycles carry over into the merged plan's `cycles` array. Prefix cycle agent names with the subtask identifier to match the node naming convention.

The most common combination is fan-out-then-pattern: a decomposer creates subtasks, each subtask runs through a complete pattern instance. For N subtasks through a debate panel, this means N independent debate subgraphs, each with their own panelists and scoring nodes. The node names must be unique across the entire plan — prefix with the subtask identifier (e.g., `subtask-1-panelist-a-round0`).

When combining patterns, the last node in the overall DAG determines `final_output`. If multiple sub-patterns produce independent final outputs, add a merge or synthesis node whose output becomes `final_output`.

**Dynamic fan-out:** If the subtask count isn't known at plan-creation time (e.g., the decomposer determines it at runtime), the orchestrator supports `dynamic_templates` — a mechanism for spawning worker nodes dynamically based on a manifest produced by an earlier node. This is an advanced feature; consult the orchestrator source for the schema.

---

After completing all custom graph specifications, return to SKILL.md Phase 2 to scaffold the run directory, generate agent files, and launch the orchestrator.
