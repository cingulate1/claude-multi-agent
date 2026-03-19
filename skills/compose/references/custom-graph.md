# Custom Graph

## When This Applies

The user wants a topology that doesn't match any single built-in pattern. This includes:
- Combining two or more patterns (e.g., decomposition feeding into debates)
- Modifying a built-in pattern's structure (e.g., adding a synthesis node after a debate)
- Specifying a completely novel topology from scratch
- Providing a Mermaid diagram or similar graph notation

## Elicit the Topology

The user may describe their graph in many forms. Accept any of these and normalize to a node-edge structure:

**Mermaid diagram** — Parse the node IDs and edges directly. Ask the user what each node does; the diagram gives you structure but not semantics.

**Freeform description** — "I want three researchers to work independently, then a critic reviews all three, then the researchers revise based on the critique." Extract the implicit DAG: 3 parallel nodes → 1 critic node → 3 parallel revision nodes.

**Pattern combination** — "Decompose the task, then run a debate on each subtask." Map this to concrete patterns: a decomposer fans out to N debate subgraphs, each with their own panelists and scoring.

**Variation on a built-in** — "Like a consensus panel, but with two rounds of independent work before the synthesis." Read the relevant pattern reference first, then modify the topology as described.

After extracting the topology, confirm it back to the user as a node list with edges before proceeding. Use a simple text format:

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

For each node in the graph, you need five pieces of information. Some the user will provide explicitly; others you should propose defaults and let the user override.

| Field | Ask or propose? | Notes |
|-------|----------------|-------|
| **Purpose** | Ask | What this node accomplishes. One sentence. |
| **Reads** | Derive from edges | The output files of its dependencies. |
| **Writes** | Propose | Default: `output/{node-name}.md`. The user rarely needs to override this. |
| **Persona** | Propose | Derive from the node's purpose. A researcher node gets a researcher persona. |
| **Tools** | Propose | Default: `Read,Write`. Add `WebSearch,WebFetch` if the task involves research, `Bash` if it involves code execution. |
| **Model** | Propose | Default: opus for reasoning-heavy nodes, sonnet for mechanical/structured work. |

Ask about purpose for every node. Propose the rest in a summary table and let the user correct any row. This minimizes back-and-forth without sacrificing control.

## Validate the Topology

Before generating, check these constraints. The orchestrator cannot handle violations — catching them here prevents silent failures.

**Acyclic or explicitly cycled.** Every cycle must be declared as a self-loop or bipartite cycle in the execution plan with a max iteration count and exit condition. If the user's graph has an implicit cycle (A → B → A), ask whether this is intentional iteration or a mistake. If intentional, ask for the exit condition and max iterations.

**Every node has at least one output.** Nodes that don't write anything are invisible to downstream nodes. If a node's purpose is purely evaluative (e.g., a critic that just scores), it still needs to write its assessment to a file.

**Parallel groups are truly independent.** Nodes in the same parallel group must not depend on each other. If the user puts two nodes in parallel but one reads the other's output, flag the contradiction.

**Script nodes vs. agent nodes.** If a node's work is mechanical (scoring, aggregation, formatting), propose making it a script node rather than an LLM agent. Script nodes are faster, cheaper, and deterministic.

## Generate Prompts for Custom Nodes

Built-in patterns have prompt templates. Custom graphs do not — you write each prompt from the node's purpose, reads, and writes. Follow this structure for every agent node:

```
You are {PERSONA_DESCRIPTION}.

## Task

{TASK_DESCRIPTION — derived from the node's purpose}

{CONTEXT_INSTRUCTION — what to read and why, derived from the node's dependencies}

{PROCEDURE — step-by-step if the task is complex, or a simple instruction if straightforward}

## Output

Write your response to {OUTPUT_PATH}.

{OUTPUT_FORMAT — what the output should contain and how it should be structured}
```

The prompt must be self-contained. The agent has no conversation history — this prompt is its entire world. Include everything it needs to do its job: what files to read, what to produce, where to write it.

For nodes that read multiple predecessors' outputs, list every file path explicitly. Do not use glob patterns or "read all files in output/" — the agent needs concrete paths.

## Build the Execution Plan

The execution plan follows the same schema as built-in patterns. For custom graphs, pay attention to `parallel_group` assignment:

- Nodes with identical dependency sets can share a parallel group.
- Nodes with different dependencies must be in different groups, even if they could theoretically run concurrently. The orchestrator runs parallel groups as atomic batches — all nodes in a group start together and the next group waits for all of them.

If the graph has nodes that could run concurrently but have different dependency sets, you may need to split them across sequential groups or restructure slightly. Explain any such adjustments to the user.

Cycles in custom graphs use the same cycle format as built-in patterns:

```json
{
  "type": "bipartite",
  "producer": "node-a",
  "evaluator": "node-b",
  "max_rounds": 5,
  "exit_signal_file": "output/exit-condition.flag"
}
```

For self-loops, the agent checks its own output against a constraint and writes the exit flag when satisfied. For bipartite cycles, the evaluator writes the flag when the producer's output meets criteria.

## Handle Pattern Combinations

When the user combines patterns, each sub-pattern retains its internal logic but connects to the others through explicit edges. The approach:

1. Identify the sub-patterns and their boundaries.
2. Read the reference for each sub-pattern involved.
3. Generate each sub-pattern's nodes using its reference's templates.
4. Add bridge nodes or edges that connect the sub-patterns.
5. Merge into a single flat execution plan — the orchestrator has no concept of nested sub-graphs.

The most common combination is fan-out-then-pattern: a decomposer creates subtasks, each subtask runs through a complete pattern instance. For N subtasks through a debate panel, this means N independent debate subgraphs, each with their own panelists and scoring nodes. The node names must be unique across the entire plan — prefix with the subtask identifier (e.g., `subtask-1-panelist-a-round0`).
