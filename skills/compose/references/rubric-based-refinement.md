# Rubric-Based Refinement

## Recommend When

- The task needs multi-dimensional quality judgment rather than optimization for a single property (chained-iteration is superior for that use case)

## Ask the User

- Which quality dimensions matter, or should the evaluator define them?
- Are there any non-negotiable constraints or categories that matter far more than the rest?
- Are there any oracles that should serve as the basis for defining the rubric?
- What baseline pass threshold (1-5) is the user comfortable with? (Default: `5/5`)
- Is the user fine with the default maximum round count? (`5`)

### Processing user feedback

- If the user states a few dimensions that matter, but doesn't fully specify the rubric:
  - <toTheUser> confirm back that the evaluator will generate the remaining rubric automatically, and that this is OK. </toTheUser>
  - <toTheEvaluator> take what the user mentioned to be the most important rubric categories. Their default pass criterion is `4/5`, or if the user was emphatic, `5/5`.</toTheEvaluator>

## Create the Right Evaluator

### Scenario 1: The user comes in with a fully-fledged rubric or, through socratic discourse, the two of you create one

In this case, the Evaluator's chief role is that of an analyst.

Include in the Evaluator's Subagent Definition (System Prompt):
- A clear sense of identity that it is not a rubber-stamp merchant.
- It is not oppressively harsh, but it doesn't give any benefit of the doubt if a provided rubric's criteria are not clearly met.

### Scenario 2: The user does not fully specify the rubric

If the user does not fully specify the rubric, the evaluator's first task will be to create/complete it.
Include in the Evaluator's Subagent Definition (System Prompt)
- To deeply consider:
  1) what constitutes successful task execution
  2) what "slop"-level "success" would look like
  3) to generate a rubric that rewards the former and punishes the latter
- A succinct and non-dramatic reminder that the creation of the rubric is the most important step in this entire workflow
- To output the rubric with no fewer than 3 and no more than 6 categories (unless <3 or >6 were specified via user edict)
  - The number of categories scales with the complexity of the output being judged
  - The evaluator makes this judgment

### In Both Scenarios:

The Evaluator's feedback does not give away the criteria for reaching a higher score on the rubric directly.

Feedback includes:
- Labeled per-dimension scores (Example: `Coherence: 2/5`)
- Explanatory guidance, written as complete sentences, providing concrete steps required to reach the next level up for each dimension
- If a revised artifact fails to increase its score in a dimension, the evaluator applies increased consideration (thinking) and verbosity to the explanatory guidance for that dimension

Feedback does NOT include:
- any explicit mention of a "rubric"

## Create the right generator

Do not mention the existence of a rubric or rubric file to the generator.

The output folder should be given a name based on the task and should not contain the word "rubric".

## Create a seed rubric for the evaluator

- Create `output/rubric-draft.md` -- a seed rubric with what the user specified and what they left unspecified, along with a complete description of what you infer to be the desired outcome and the nature of the generator's task
- The generator's task instructions live in its subagent definition entirely, it gets no equivalent file

## Tool Assignments

| Subagent | Tools |
|----------|-------|
| Generator | `Read,Write` |
| Evaluator-init (Scenario 1: full rubric) | `Read,Write` |
| Evaluator-init (Scenario 2: incomplete rubric) | `Read,Write,WebSearch,WebFetch` |
| Evaluator (cycle) | `Read,Write` |

Since evaluator-init and the cycle evaluator use the same agent file, the orchestrator passes different `--tools` at each invocation. The agent file's `tools` frontmatter should list the union needed for init; the cycle invocation restricts to `Read,Write`.

## Generate The Workflow Topology

Three nodes, two agents:
- `evaluator-init`: uses the evaluator agent file. Runs once, standalone. No cycle membership.
  - Scenario 1: reads the provided `output/rubric.md` (already written by Claude during Phase 2) and validates it.
  - Scenario 2: reads `output/rubric-draft.md` (the seed) and creates `output/rubric.md`.
- `generator`: uses the generator agent file. Depends on `evaluator-init`. Writes the primary artifact.
- `evaluator`: uses the same evaluator agent file. Depends on `generator`. Grades the artifact and writes `output/evaluation-feedback_{counter}.md` (counter increments from 1).

Create one bipartite cycle between generator and evaluator:
  - `type`: `bipartite`
  - `producer`: generator
  - `evaluator`: evaluator
  - `max_rounds`: agreed limit
  - NOTE: `max_rounds` of 5 means 5 evaluation rounds — the generator produces 6 total turns (initial + 5 revisions)
  - `exit_signal_file`: `output/evaluation-pass.flag`

`final_output` is the generator's primary artifact.

## Agent Prompt: Generator

The generator prompt must not mention rubrics, scores, or evaluation criteria.

```
## Task

{TASK_DESCRIPTION}

{CONTEXT_INSTRUCTION}

## Procedure

1. Check if feedback exists at output/evaluation-feedback_{latest}.md. If so, read it.
2. If no feedback exists, produce the initial draft.
3. If feedback exists, address the guidance provided for each dimension. Focus on the areas flagged as needing the most improvement.

## Output

{OUTPUT_FORMAT}

Write your artifact to {ARTIFACT_PATH}.
```

## Evaluator Prompt Templates

The evaluator agent file is used for both the `evaluator-init` node and the `evaluator` cycle node, but they receive different prompts. Provide separate prompt files for each.

### Evaluator-Init Prompt (Scenario 1 — Full Rubric Provided)

In Scenario 1, `output/rubric.md` already exists (written by Claude during Phase 2). The init step validates it.

```
## Task

Read the rubric at output/rubric.md. Verify that:
- Every dimension has clear scoring criteria for each level (1-5)
- Pass thresholds are explicitly stated per dimension
- Dimensions are non-overlapping and collectively cover the quality space

If any dimension lacks clear differentiation between score levels, refine the criteria and overwrite output/rubric.md with the improved version.

Do not evaluate any artifact. Your only task is rubric validation.

## Output

Write a brief validation summary to output/rubric-validation.md noting any adjustments made.
```

### Evaluator-Init Prompt (Scenario 2 — Rubric Not Fully Specified)

In Scenario 2, `output/rubric-draft.md` exists (a seed with partial criteria and task context). The init step creates the full rubric.

```
## Task

Read the seed at output/rubric-draft.md. It contains partial criteria from the user and a description of the generator's task.

Before writing anything, deeply consider:
1. What does genuinely successful execution of this task look like?
2. What would superficially-adequate but actually poor ("slop-level") output look like?
3. What rubric would reliably reward the former and penalize the latter?

Generate a rubric with no fewer than 3 and no more than 6 dimensions. The number of dimensions should scale with the complexity of the output being judged — use your judgment. For any dimensions the user explicitly specified, set their pass threshold to {USER_SPECIFIED_THRESHOLD}/5. For dimensions you define, set the pass threshold to {DEFAULT_THRESHOLD}/5.

The creation of this rubric is the most important step in the entire workflow. Take the time to get it right.

Do not evaluate any artifact. Your only task is rubric creation.

## Output

Write the completed rubric to output/rubric.md.
```

### Evaluator Cycle Prompt (Both Scenarios)

Used for every cycle invocation of the evaluator after init completes. The rubric at `output/rubric.md` is guaranteed to exist at this point.

```
## Task

Evaluate the artifact at {ARTIFACT_PATH} against the rubric at output/rubric.md.

## Evaluation Procedure

1. Score the artifact on each rubric dimension (1-5).
2. For each dimension, write concrete guidance on what specific changes would reach the next level up. Do not reveal the rubric criteria directly — frame guidance as actionable steps.
3. If a dimension's score has not improved since the last round, increase your thinking effort and the specificity of your guidance for that dimension.

### Pass Decision

If all dimensions meet their pass thresholds: write the file `output/evaluation-pass.flag` containing the word "PASS".
Otherwise: do NOT write the flag.

## Output

Format:
- Per-dimension score (e.g., `Coherence: 2/5`)
- Explanatory guidance for each dimension as complete sentences
- Do not mention the word "rubric" in the feedback

Write your evaluation to output/evaluation-feedback_{COUNTER}.md.
```
