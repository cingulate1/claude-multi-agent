# Debate Panel

## Recommend When

- The user wants the answer that best survives adversarial critique.
- A stable minority answer should be allowed to beat a conformist majority.
- The task has discrete candidate answers (a choice, a diagnosis, a strategy, a recommendation) rather than a continuous artifact to polish.
- Structured challenge matters more than synthesis.

## Ask the User

- How many panelists? Default: `3`. Minimum: `3`.
- What distinct perspectives or expertise should the panelists bring?
- What exact decision, argument, or deliverable should they contest?
- How many debate rounds? Default: `2`. Range: `1`-`3`. More rounds increase token cost and risk conformity drift; fewer rounds may not surface all weaknesses. One round is sufficient for most tasks.
- How many evaluation samples per scoring question? Default: `3`. Range: `1`-`7`. More samples reduce variance in Haiku's semantic judgments but increase scoring time linearly. `3` is sufficient for most tasks.
- Answer format: panelists should include a `## Final Answer` block at the end of their output. The scoring system uses Haiku-based semantic evaluation to assess position changes and convergence across rounds, so answers do not need to be short labels — longer-form answers work fine.
- If the user wants more than 3 rounds, note that token cost scales as `O(N * R)` and conformity risk increases with each round. Recommend staying at 1-2 rounds unless the task specifically benefits from extended deliberation.

## Confirm Back

- `N` panelists. Each panelist uses one agent file but is instantiated across multiple phases.
- `R` debate rounds (default `2`). The topology has `R + 1` phases of panelist work plus one final scoring phase.
- Phase 0 is independent answer generation. Phases 1 through R are adversarial debate rounds. The final phase is semantic evaluation (Haiku) followed by deterministic scoring.
- The winner is selected by a two-stage scoring pipeline: Haiku evaluates semantic position changes and convergence across rounds (`S` samples per evaluation, SHA-256 seeded), then a deterministic algorithm computes final scores from those evaluations.
- Panelists are never told how the winner is selected. Their prompts contain zero information about the scoring mechanism.
- Final output is the highest-scoring answer, copied verbatim from the panelist that produced it.

## The Scoring Mechanism

Scoring uses a two-stage pipeline: **semantic evaluation** (Haiku LLM calls) followed by **deterministic scoring** (pure math).

### Stage 1: Semantic Evaluation

For each round transition (k-1 → k), for each panelist, independent Haiku invocations answer two questions:

- **Q1**: To what extent did this panelist change their core position? (1-5 scale)
- **Q2**: To what extent did each other panelist move toward this panelist's position? (1-5 scale)

Each evaluation is an independent Haiku call with no knowledge of the larger workflow. A SHA-256 hash of the evaluation index is prepended as a `<taskID>` to seed distinct inference contexts across repeated samples (default: 3 samples per evaluation, median aggregated).

The semantic approach means panelists can express answers in any form — paragraphs, structured arguments, labels — and the scoring system will correctly detect position changes, convergence, and maintenance regardless of phrasing.

### Stage 2: Deterministic Scoring

The evaluation scores are mapped to coefficients (1→0.00, 2→0.25, 3→0.50, 4→0.75, 5→1.00) and combined per panelist per transition:

```
change_component    = Q1_avg × -1 × 1.5
convergence_component = sum(Q2_avg_j for each other panelist j)
element_score       = (change + convergence) × f
```

Where `f = 1 / (k + 1)` is the round decay factor (same as Free-MAD). The 1.5 multiplier on change preserves the `(w2 + w4) / w3 = 45/30` ratio from the discrete Free-MAD weights.

The panelist with the highest total score wins. Their final-round answer is copied verbatim to the output.

### Why This Design

- Semantic evaluation means the scoring system understands *meaning*, not just string identity. Two panelists expressing the same conclusion in different words are correctly recognized as agreeing.
- The SHA-256 task ID decorrelates repeated samples — each Haiku call sees a unique "task context" that prevents correlated scoring artifacts across samples.
- Haiku is used for scoring (cheap, fast) while the debate itself runs on a reasoning-heavy model. The scorer doesn't need to be smart — it just needs to rate change/convergence on a 1-5 scale.
- The decay factor counteracts LLM conformity: opinion shifts in later rounds carry less weight than earlier, more independent judgments.
- The deterministic scoring stage runs outside the LLM. The Haiku calls only produce atomic 1-5 integers — the aggregation math cannot be influenced by hallucination or manipulation.

### Scoring Scripts

Two scripts are bundled at `${CLAUDE_PLUGIN_ROOT}/skills/compose/scripts/`:

1. `run_semantic_evals.py` — Invokes Haiku to produce evaluation CSVs in `output/evaluations/`. Default: 3 samples per evaluation.
2. `score_debate_semantic.py` — Reads the CSVs, builds coefficient matrices, computes final scores, writes `output/final-selection.md`.

Copy both into the run directory at scaffold time. The orchestrator runs them as two sequential script nodes after the final debate round.

## Tool Assignments

| Subagent | Tools |
|----------|-------|
| Panelist (all rounds) | `Read,Write` |
| Scorer | N/A (script node, not an LLM agent) |

## Generate This Topology

### Phases

- **Phase 0** (parallel group `initial`): all `N` panelists write `output/{persona}-round0.md` independently. No dependencies.
- **Phases 1 through R** (parallel group `round-{k}`): each panelist depends on ALL panelist outputs from the previous round. Panelist `i` in round `k` reads every `output/*-round{k-1}.md` and writes `output/{persona}-round{k}.md`.
- **Evaluation phase** (single node `semantic-evaluator`): depends on all round-R outputs. Runs `run_semantic_evals.py` to invoke Haiku for semantic evaluation of position changes and convergence. Writes CSV files to `output/evaluations/`.
- **Scoring phase** (single node `scorer`): depends on `semantic-evaluator`. Runs `score_debate_semantic.py` to compute final scores from the evaluation CSVs. Writes `output/final-selection.md`.

### Nodes

For `N` panelists and `R` rounds, generate `N * (R + 1) + 2` nodes total:

- `N` initial nodes: `{persona}-round0` (parallel group `initial`, no dependencies)
- `N` debate nodes per round: `{persona}-round{k}` for k = 1..R (parallel group `round-{k}`, depends on all `*-round{k-1}` nodes)
- `1` semantic evaluator node: `semantic-evaluator` (depends on all round-R nodes)
- `1` scorer node: `scorer` (depends on `semantic-evaluator`)

Each debate-round node uses the same agent file as the corresponding panelist's initial node — the persona is consistent across rounds.

### Scorer Nodes

The scoring pipeline has two sequential script nodes — neither is an LLM agent (though the first invokes Haiku internally):

1. **`semantic-evaluator`**: Runs `run_semantic_evals.py` with `--samples S`. Spawns independent Haiku calls to evaluate position changes and convergence across rounds. Writes CSV files to `output/evaluations/`.
2. **`scorer`**: Runs `score_debate_semantic.py` with `--samples S`. Reads the CSVs, computes final scores, writes `output/final-selection.md`.

`S` is the evaluation sample count chosen during configuration (default `3`). Both scripts must receive the same value. The `--samples` argument is passed via the `script_args` field in the execution plan node definition.

### No Cycles

This topology is acyclic. The multi-round debate structure is expressed as explicit sequential phases, not as bipartite cycles. Each round is a distinct set of nodes with explicit dependencies on the prior round's outputs.

### `final_output`

`output/final-selection.md`

## Agent Prompt: Phase 0 Panelist

One prompt file per panelist. Panelist prompts must NOT mention scoring, weights, points, winning, losing, selection mechanisms, or any external judging process.

```
You are {PERSONA_DESCRIPTION}.

## Task

{DEBATE_QUESTION}

{CONTEXT_INSTRUCTION}

Analyze this question independently. Present your reasoned argument, then state your final answer.

## Output

Your response must end with a clearly delimited answer block:

## Final Answer
[Your answer here]

All reasoning and analysis appears above the Final Answer block.

Write your response to {OUTPUT_PATH}.
```

## Agent Prompt: Debate Round Panelist

Same agent file as the Phase 0 panelist — the persona carries across rounds. One prompt file per panelist per round. These prompts must NOT mention scoring, weights, points, winning, losing, answer tracking, or any external selection process.

```
You are {PERSONA_DESCRIPTION}.

## Task

{DEBATE_QUESTION}

You are in round {K} of a structured debate. Read all participants' responses from the previous round:
{LIST_ALL_PREVIOUS_ROUND_OUTPUT_PATHS}

Follow this procedure exactly:

1. **State your current position.** Restate your answer and core reasoning from the previous round.
2. **Analyze each peer's reasoning individually.** For each peer, identify whether their reasoning is sound or contains errors. Name the specific errors — do not make generic comments like "this seems weak." If you cannot find a concrete flaw, say so explicitly.
3. **Compare with your own reasoning.** Check whether you have made errors similar to those you identified in peers, or errors they identified in you.
4. **Decide whether to revise.** Change your answer ONLY if you find clear evidence that your own reasoning is wrong. Do not change your answer because a majority disagrees with you. Majority agreement is not evidence of correctness.
5. **Provide your final answer** in the `## Final Answer` block.

You may not rely on conformity. If you cannot determine whether others are correct, retain your own conclusion.

## Output

Your response must end with:

## Final Answer
[Your answer here]

Write your response to {OUTPUT_PATH}
```

## Panelist Blindness

Panelist prompts must NOT contain:
- Any mention of scoring, weights, points, or selection mechanisms
- Any mention of "winning" or "losing" the debate
- Any suggestion that answer changes are tracked or scored
- Any hint that later rounds carry less weight
- Any reference to an external selection process

Panelists must believe they are in a genuine adversarial debate where the strength of their reasoning is what matters.

## Scorer Nodes (No Agent Files, No Prompt Files)

The scoring pipeline uses two sequential script nodes. Do NOT generate agent files or prompt files for them. The execution plan entries are all that's needed:

```json
{
  "name": "semantic-evaluator",
  "node_type": "script",
  "script": "run_semantic_evals.py",
  "script_args": ["--samples", "<S>"],
  "depends_on": ["<all round-R node names>"],
  "outputs": []
},
{
  "name": "scorer",
  "node_type": "script",
  "script": "score_debate_semantic.py",
  "script_args": ["--samples", "<S>"],
  "depends_on": ["semantic-evaluator"],
  "outputs": ["output/final-selection.md"]
}
```
