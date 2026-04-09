# Dissensus Integration

## Recommend When

- The user wants comprehensive coverage from multiple complementary lenses.
- Unique contributions matter more than overlap.
- Tension between perspectives should be integrated rather than eliminated.

## Ask the User

- How many panelists? Default: `3`.
- What distinct lens or perspective does each panelist bring?
- What final integrated artifact should the workflow produce?

## Confirm Back

- `N` complementary panelists plus `1` integrator.
- Panelists work independently; they are not refining each other.
- The integrator combines what is unique from each perspective rather than extracting consensus.

## Tool Assignments

| Subagent | Tools |
|----------|-------|
| Panelist | `Read,Write` |
| Integrator | `Read,Write` |

## Generate This Topology

- Phase 1: all panelists write `output/{persona}-initial.md` in parallel.
- Phase 2: the integrator depends on all panelist outputs, reads every `*-initial.md`, and writes `output/integrated-synthesis.md`.

`final_output`: `output/integrated-synthesis.md`

## Agent Prompt Templates

Note: Every template below ends with the validator-enforced line `Write your output to {ABSOLUTE_OUTPUT_PATH}`. Substitute only the path placeholder; preserve the rest verbatim. See SKILL.md "Mandatory Final Line" for the full rule.

## Agent Prompt: Panelist

One prompt file per panelist. Choose lenses that are genuinely different rather than redundant.

```
You are {PERSONA_DESCRIPTION}.

## Task

{TASK_DESCRIPTION}

Read the following for context:
{CONTEXT_FILES}

Analyze this from your specific perspective. Focus on what your lens sees that others would likely miss. Do not attempt to be comprehensive across all dimensions — go deep on your area of expertise.

## Output

{OUTPUT_FORMAT}

Do not read any other agent's output.

Write your output to {ABSOLUTE_OUTPUT_PATH}
```

## Agent Prompt: Integrator

```
You are an integrator combining distinct expert perspectives into a unified artifact.

## Task

{N} experts each analyzed the following from their own lens:

{TASK_DESCRIPTION}

Read all expert outputs:
{LIST_ALL_PANELIST_OUTPUT_PATHS}

Also read the original context:
{CONTEXT_FILES}

## Instructions

- Harvest what is unique from each perspective. Do not collapse distinct insights into generic agreement.
- Preserve useful tension between perspectives — if experts disagree, present both views rather than averaging them.
- Structure the output so that each perspective's contribution is identifiable but woven into a coherent whole.
{STYLE_MATCHING_INSTRUCTION}

## Output

{OUTPUT_FORMAT}

Write your output to {ABSOLUTE_OUTPUT_PATH}
```
