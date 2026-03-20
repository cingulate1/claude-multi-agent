# Consensus Panel

## Recommend When

- The user wants real common ground across distinct perspectives.
- Overlap is the signal being extracted.
- Unresolved disagreement should be filtered out rather than preserved.

## Ask the User

- How many panelists? Default: `3`.
- Who is each panelist as a persona or worldview?
- What shared question or deliverable should all panelists address?

## Confirm Back

- `N` distinct personas, not interchangeable roles.
- `2N+1` agents total: `N` initial panelists, `N` refinement panelists, `1` synthesizer.
- Final output is a consensus synthesis, not a vote tally and not a catalog of all views.

## Tool Assignments

| Subagent | Tools |
|----------|-------|
| Initial Panelist | `Read,Write` |
| Refinement Panelist | `Read,Write` |
| Synthesizer | `Read,Write` |

## Generate This Topology

- Phase 1: all panelists write `output/{persona}-initial.md` in parallel.
- Phase 2: all refinement panelists depend on all Phase 1 outputs, read every `*-initial.md`, and write `output/{persona}-refine.md` in parallel.
- Phase 3: the synthesizer depends on all refined outputs, reads every `*-refine.md`, and writes `output/consensus-synthesis.md`.

`final_output`: `output/consensus-synthesis.md`

## Agent Prompt: Initial Panelist

One prompt file per panelist. Define the panelist by who they are, not by a generic job label.

```
You are {PERSONA_DESCRIPTION}.

## Task

{SHARED_QUESTION}

Read the following for context:
{CONTEXT_FILES}

Produce your independent analysis from your perspective. Write what you believe is most important and correct. Do not attempt to anticipate or accommodate other perspectives.

## Output

{OUTPUT_FORMAT}

Do not read any other agent's output.

Write your response to {OUTPUT_PATH}.
```

## Agent Prompt: Refinement Panelist

One prompt file per panelist. Same persona as the corresponding initial panelist.

```
You are {PERSONA_DESCRIPTION}. You previously wrote an initial analysis.

## Task

{SHARED_QUESTION}

{N} experts independently addressed this question. Read all initial analyses:
{LIST_ALL_INITIAL_OUTPUT_PATHS}

Also re-read the context:
{CONTEXT_FILES}

Revise your recommendations:
- Update where genuinely persuaded by another expert's reasoning.
- Drop points that now seem lower-value after seeing the full picture.
- Sharpen points that were validated by overlap with other experts.
- Maintain your own perspective — do not drift into generic advice.

## Output

{OUTPUT_FORMAT}

Write your revised response to {OUTPUT_PATH}.
```

## Agent Prompt: Synthesizer

```
You are a synthesis editor extracting consensus from multiple expert perspectives.

## Task

{N} experts each independently analyzed and then refined their recommendations on the following question:

{SHARED_QUESTION}

Read all refined outputs:
{LIST_ALL_REFINE_OUTPUT_PATHS}

Also read the original context:
{CONTEXT_FILES}

## Instructions

- Include only points where at least two experts converge (same core advice, even if worded differently).
- Points where all experts converge are high-priority — give them prominence.
- Remaining divergence may be noted briefly but should not drive the output.
{STYLE_MATCHING_INSTRUCTION}

## Output

{OUTPUT_FORMAT}

Write the consensus synthesis to {OUTPUT_PATH}.
```
