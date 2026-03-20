# Chained Iteration

## Recommend When

- One agent can do the work.
- Success is objectively self-checkable.
- The likely failure mode is missing a concrete constraint on the first pass.

Use a different pattern if quality is subjective (`rubric-based`), facts need source verification (`rag-grounded`), or multiple perspectives matter (panel patterns).

## Ask the User

- What exact constraint must the output satisfy?
- How can Claude verify that constraint mechanically?
- What is the maximum iteration count? Default: `3`.

## Confirm Back

- One writer/refiner agent.
- One primary output artifact.
- Self-loop up to the agreed iteration limit.
- Exit condition: the agent writes `output/constraint-met.flag` only when the constraint is satisfied.

## Tool Assignments

| Subagent | Tools |
|----------|-------|
| Writer/Refiner | `Read,Write` |

## Generate This Topology

- Create one node with no dependencies.
- Create one self-loop cycle:
  - `type`: `self-loop`
  - `agent`: the writer/refiner node
  - `max_iterations`: agreed limit
  - `exit_signal_file`: `output/constraint-met.flag`
- `final_output` is the primary artifact written by that node.

## Agent Prompt: Writer/Refiner

```
## Task

{TASK_DESCRIPTION}

{CONTEXT_INSTRUCTION}

## Constraint

Your output must satisfy the following:
{CONSTRAINT_DESCRIPTION}

## Procedure

1. Check if a prior draft exists at {ARTIFACT_PATH}. If so, read it and the constraint check results.
2. If no prior draft exists, produce the initial version.
3. If a prior draft exists, make surgical revisions to address the specific constraint violations. Do not replace good content wholesale.
4. After writing/revising, verify the constraint yourself.
5. If the constraint is satisfied, write the file `output/constraint-met.flag` containing the word "PASS".
6. If the constraint is NOT satisfied, do not write the flag. Describe what remains unmet so your next iteration can address it.

## Output

{OUTPUT_FORMAT}

Write your artifact to {ARTIFACT_PATH}.
```
