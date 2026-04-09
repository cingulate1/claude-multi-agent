# RAG-Grounded Refinement

## Recommend When

- The output makes factual or methodological claims that must be checked against authoritative sources.
- A generator and a verifier should stay separate.
- Source-grounded revision is more important than subjective polish.

## Ask the User

- What sources should the evaluator verify against: local files/directories, web, or both?
- What kinds of claims or requirements should the evaluator check?
- Should the generator read the sources directly, or should grounding be evaluator-only?
- Is any methodology or source location intentionally hidden from the generator?
- What is the maximum round count? Default: `5`.

## Confirm Back

- Two agents: generator and evaluator.
- The source location(s) and verification focus.
- If evaluator-only or hidden-methodology is requested, state separately what the generator may see and what only the evaluator may see.
- Exit condition: evaluator writes `output/evaluation-pass.flag` only when no material issues remain.

## Tool Assignments

| Subagent | Tools |
|----------|-------|
| Generator | `Read,Write` |
| Evaluator (local sources) | `Read,Write,Glob,Grep` |
| Evaluator (web sources) | `Read,Write,WebSearch,WebFetch` |
| Evaluator (both) | `Read,Write,Glob,Grep,WebSearch,WebFetch` |

Select the evaluator's tool set based on the user's answer to "What sources should the evaluator verify against: local files/directories, web, or both?"

## Generate This Topology

- Generator node writes the primary artifact.
- Evaluator node depends on the generator and writes `output/evaluation-feedback.md`.
- Create one bipartite cycle:
  - `type`: `bipartite`
  - `producer`: generator
  - `evaluator`: evaluator
  - `max_rounds`: agreed limit
  - `exit_signal_file`: `output/evaluation-pass.flag`
- `final_output` is the generator's artifact.

## Agent Prompt: Generator

If hidden-methodology is active, the generator prompt must not mention the framework name, source path, corpus, or methodology language anywhere.

Note: The final line below is validator-enforced — substitute only the path placeholder, preserve the rest verbatim. See SKILL.md "Mandatory Final Line" for the full rule.

```
## Task

{TASK_DESCRIPTION}

{CONTEXT_INSTRUCTION — only what the generator is allowed to see}

## Procedure

1. Check if evaluator feedback exists at output/evaluation-feedback.md. If so, read it.
2. If no feedback exists, produce the initial draft.
3. If feedback exists, address every issue raised before rewriting. Do not skip any flagged issue.

## Output

{OUTPUT_FORMAT}

Write your output to {ABSOLUTE_OUTPUT_PATH}
```

## Agent Prompt: Evaluator

The evaluator prompt must instruct assessment across two tiers, in order. Both tiers must be satisfied before the pass flag can be written.

Note: The final line below is validator-enforced — substitute only the path placeholder, preserve the rest verbatim. See SKILL.md "Mandatory Final Line" for the full rule. `{GENERATOR_OUTPUT_PATH}` is the generator's absolute output path; `{ABSOLUTE_OUTPUT_PATH}` is the evaluator's own absolute output path (a different file).

```
## Task

Evaluate the artifact at {GENERATOR_OUTPUT_PATH} against the source material.

## Sources

{SOURCE_PATHS_AND_RETRIEVAL_INSTRUCTIONS}

## Evaluation Procedure

Assess the artifact in two tiers, in order. Complete Tier 1 fully before beginning Tier 2.

### Tier 1 — Factual Fidelity

Check every factual claim in the artifact against the source corpus. Flag any claim that:
- Contradicts the source
- Misrepresents the source
- Softens an exact value with hedging language
- Cannot be traced to any source

### Tier 2 — Source Utilization and Structural Quality

{TASK_SPECIFIC_TIER_2_CRITERIA}

Check the following:
- **Coverage**: Are there significant areas of the source corpus that the artifact ignores? Identify specific sections or passages from the sources that are absent but relevant.
- **Depth of synthesis**: Does the artifact integrate ideas across source sections, or merely restate them in sequence?
- **Fitness for purpose**: Given the stated task, does the artifact actually serve that purpose?

### Pass Decision

After completing both tiers in full, decide:
- If zero factual errors remain AND no significant coverage, synthesis, or fitness issues remain: write the file `output/evaluation-pass.flag` containing the word "PASS".
- Otherwise: do NOT write the flag.

## Output

For every issue found in either tier, state:
1. What is wrong
2. Where the relevant source material is
3. What the generator should do differently

Write your output to {ABSOLUTE_OUTPUT_PATH}
```

## Hard Boundary for Evaluator-Only or Hidden-Methodology Setups

- The generator agent file must not mention the framework name, source path, corpus, source documentation, or methodology language anywhere.
- That boundary applies to both YAML frontmatter `description` and the markdown body.
- The generator should see only the visible artifact/task plus "revise from evaluator feedback."
- The evaluator alone gets the source path, retrieval instructions, hidden methodology, and pass criteria.
- Before launch, check the generated agent files and fix any leakage before proceeding.
