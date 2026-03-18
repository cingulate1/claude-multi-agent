# Improvement Notes

## Absolute Pathing in Prompts

All file paths in agent prompts must be absolute. Relative paths resolve against the agent's CWD, which may differ from the run directory depending on how the orchestrator launches agents. The first run of the hybrid pattern failed because synthesizers wrote to the CWD while panelists expected files in the run directory. Absolute paths eliminate this class of failure entirely.

## Copy Winning Output to Top-Level

After a run completes, copy the final output file (e.g., `output/final-selection.md` or the winning synthesis it references) to the top-level output folder the user is working from — not buried inside a `debate-run/output/` subdirectory. The user shouldn't have to navigate nested run directories to find the deliverable.

## Encourage Extended Thinking for Reasoning-Heavy Nodes

For reasoning-heavy agents (synthesizers, evaluators, panelists — anything that isn't mechanical), the prompt must prime extensive reasoning. There are two complementary mechanisms, and **both are necessary**.

### 1. The prompt must embody the verbosity it wants

Core principle: "The message should embody its desired output." The model's output is conditioned on the full preceding token sequence. A terse 15-line prompt that says "think extensively" is a contradiction — a terse primer primes terse generation.

For reasoning-heavy nodes, the prompt itself should be written in the expansive, analytical register the agent should adopt. This means multiple paragraphs walking through the nuances of the task, the specific tensions worth exploring, the kinds of tradeoffs at play, the failure modes of naive approaches. The prompt models the depth of thinking, and the agent continues in that mode.

This was the likely cause of the token disparity in the sycophancy synthesis run: the "systems architect" persona (9k output tokens) received the same terse prompt as the "measurement theorist" (21k output tokens). The architect's persona values — conciseness, clean interfaces, structural clarity — compounded the terseness of the prompt, producing a convergent effect. The measurement theorist's values — questioning validity, enumerating failure modes — naturally resist terseness even when the prompt doesn't model verbosity. But a verbose, analytically rich prompt would have pushed the architect toward deeper reasoning too.

The cost: every reasoning-heavy agent prompt becomes a small essay. For a graph with many reasoning-heavy nodes, this is a real authoring burden. It also burns input tokens on every invocation.

### 2. Explicit thinking scaffolds

In addition to the prompt embodying verbosity, provide structured reasoning stages using sections that **don't mirror the expected output** but instead mirror a skillful reasoning process:

```
Before writing your output, work through these reasoning stages:

<source_analysis>
What does each source document actually say? What are the key claims, structures, and design decisions in each? Be thorough — surface-level reading produces surface-level synthesis.
</source_analysis>

<comparative_evaluation>
Where do the sources agree? Where do they conflict? Which conflicts represent genuine design tradeoffs vs. one source simply being better? Don't rush to resolve tensions — sit with them and understand what each side gets right.
</comparative_evaluation>

<synthesis_strategy>
What organizing principle will unify the best elements? What must be reconciled vs. what can be adopted wholesale? What would be lost by each possible approach?
</synthesis_strategy>

Then write your output.
```

The scaffold sections should guide the *reasoning process*, not pre-structure the *output*. If sections mirror the output structure, the agent just drafts the output twice. If they mirror the reasoning process (analyze, compare, strategize, then write), the agent does genuinely deeper work before committing to a structure.

Note: even the scaffold text itself should model verbosity — "Be thorough — surface-level reading produces surface-level synthesis" does more than just the bare `<source_analysis>` tag.

### 3. The structural incompatibility problem (HARD)

The model will "lazily" migrate thinking-budget instructions into output-budget generation unless thinking scaffolds are **structurally incompatible with the expected output format**. This is analogous to CLIP/Stable Diffusion composition: if you prompt for a "full body portrait," the model may crop at the neck. You need to include elements that can only appear if the full body is in frame — "hairband," "halo," "top-knot" — which structurally force the composition by requiring space above the head.

The same principle applies here. If your thinking scaffolds contain material that *could plausibly appear in the output*, the model will fold them into the output and skip the actual thinking. `<source_analysis>` is vulnerable to this — a polished design document could easily contain a source analysis section. The scaffold needs to demand things that would be *wrong* to include in the final deliverable:

Elements that are structurally incompatible with polished output:
- **Self-doubt and uncertainty**: "Where am I least confident? What am I probably wrong about here?"
- **Dead-end exploration**: "Explore an approach you think will fail, and articulate exactly why it fails."
- **Adversarial self-critique**: "Argue against your emerging synthesis. What would a hostile reviewer say?"
- **Explicit scoring/ranking of rejected alternatives**: "Score each option 1-10 on three dimensions before choosing."
- **Confession of bias**: "Which source are you instinctively drawn to? Why? Is that preference justified or just aesthetic?"

These can only live in thinking space because they'd be inappropriate in a confident design document. A polished synthesis doesn't contain "I'm not sure about this" or "here's an approach I tried and discarded" — so the model can't lazily merge them into output.

This is extremely hard to get right in general. The scaffold must be tailored not just to the reasoning process but to the *specific output format* — you need to know what the output looks like to know what's incompatible with it. And even well-designed scaffolds can fail if the model decides the "thinking" sections are themselves the deliverable.

This remains the central open problem for reasoning-heavy agents in this plugin.

### Open Questions

- **Per-persona thinking scaffolds**: The measurement theorist might benefit from `<validity_audit>` and `<failure_mode_analysis>` sections, while the systems architect benefits from `<boundary_identification>` and `<interface_design>`. Persona-aligned scaffolds could amplify each persona's strengths rather than applying a one-size-fits-all template. But this further increases the authoring burden — each agent gets a bespoke prompt essay plus bespoke reasoning scaffolds.

- **Token budget hints**: Explicitly saying "spend at least N tokens reasoning before writing" vs. just providing the scaffold and letting the agent decide. Risk of the former: padding. Risk of the latter: some personas naturally converge fast (as observed). The prompt-embodiment approach may be a better lever than explicit token targets, since it shapes the *character* of reasoning rather than just its *volume*.

- **Thinking vs. output token ratio**: The 21k-thinking / 536-line-output synthesizer won the debate. Is ~40:1 a meaningful signal, or incidental at n=1? Worth tracking across runs.

- **Interaction with debate scoring**: If prompt verbosity equalizes token usage across personas, does that change debate outcomes? The current result may partly reflect "which agent thought harder" rather than "which persona produced the best design." Equalizing thinking effort would isolate the persona variable.

- **Compose-time generation**: Could the compose phase itself generate verbose, persona-appropriate prompt essays rather than requiring the orchestrator author to write them by hand? The compose agent knows the task, the persona, and the pattern — it could draft an expansive prompt in the register of each persona. This would solve the authoring burden at the cost of one more LLM call per agent during scaffolding.
