# Internal Consistency Review Plan

**Plugin**: claude-multi-agent
**Trigger**: v1.3.0 post-mortem — agents silently ran on opus when frontmatter said haiku; `--dangerously-skip-permissions` blocked writes inside `.claude/` cache; stale node counts in SKILL.md after scoring pipeline split.

**Purpose**: Catch every class of "the surface says X but the machinery does Y" bug before it ships. Each check is a self-contained prompt runnable in an independent Claude Code context. Results are written to handoff files in `review/` so downstream checks can reference upstream findings.

---

## Setup

```bash
mkdir -p review
```

Each reviewer writes its findings to `review/{check-number}-{slug}.md`. If a check finds zero issues, it writes a one-line `PASS` file. If it finds issues, it lists them with file paths and line numbers.

---

## Check 1: Model Propagation Chain

**Handoff file**: `review/01-model-propagation.md`

**Prompt**:
```
You are auditing the model propagation chain in a Claude Code plugin. Trace the full lifecycle of the `model` field from creation to execution:

1. Read skills/compose/scripts/create-subagent.py — what value does it write to frontmatter?
2. Read scripts/orchestrator.py — find _read_agent_frontmatter and _build_agent_cmd. Does the model value from frontmatter actually reach the `claude` CLI command as `--model <value>`? Are there any code paths where it gets dropped?
3. Read scripts/graph_monitor.py — find the frontmatter model extraction and _normalize_model_label. Does the GUI display the same model the orchestrator passes to the CLI?
4. Read scripts/status_tracking.py — find _read_frontmatter_model and _normalize_model_label. Does the status tracker record the same model the orchestrator passes?
5. Read skills/compose/scripts/run_semantic_evals.py — find invoke_haiku(). Is the model hardcoded correctly? Does it match what debate-panel.md says?

For each link in the chain, confirm the value is faithfully propagated or document the break. Write findings to review/01-model-propagation.md.
```

---

## Check 2: CLI Flag Audit

**Handoff file**: `review/02-cli-flags.md`

**Prompt**:
```
You are auditing every `claude` CLI invocation in this plugin for correctness and safety.

1. Read scripts/orchestrator.py — find every place a `claude` command is built (look for cmd = ["claude", ...] patterns). For each invocation, list all flags.
2. Read skills/compose/scripts/run_semantic_evals.py — find the invoke_haiku() function. List all flags.
3. For each invocation found:
   a. Does it include --dangerously-skip-permissions? It MUST NOT (the user's auto-approve hook handles permissions; dangerous mode blocks writes inside ~/.claude/).
   b. Does it pass --model when a model is specified?
   c. Are there any flags that could silently change behavior if the user's environment differs (e.g., flags that assume a specific permission mode)?

Write findings to review/02-cli-flags.md.
```

---

## Check 3: Script Arguments — Reference vs. Implementation

**Handoff file**: `review/03-script-args.md`

**Prompt**:
```
You are auditing whether execution plan templates in reference docs match the actual argparse definitions in scripts.

For each script node type in the plugin:

1. Read skills/compose/references/debate-panel.md — find the execution plan template JSON for semantic-evaluator and scorer nodes. Extract the script name and script_args.
2. Read skills/compose/scripts/run_semantic_evals.py — find the argparse definition in main(). List every argument (positional and optional), its type, and default.
3. Read skills/compose/scripts/score_debate_semantic.py — same.
4. Read scripts/orchestrator.py — find run_script(). How does it invoke scripts? Does it pass run_dir as a positional arg, via cwd, or both? Does it forward script_args correctly?

Verify:
- Every arg in the reference template is accepted by the script's argparse
- No required positional args exist that the orchestrator doesn't provide
- Default values in scripts match defaults documented in references
- The cwd convention is consistent (scripts use Path.cwd(), orchestrator sets cwd=run_dir)

Write findings to review/03-script-args.md.
```

---

## Check 4: Numeric Constants and Formulas

**Handoff file**: `review/04-numeric-constants.md`

**Prompt**:
```
You are auditing cross-file numeric constants for consistency.

Check each of the following:

1. **Node count formulas**: Read skills/compose/SKILL.md and every file in skills/compose/references/. For each pattern, extract the documented node/agent count formula. Verify they agree between SKILL.md and the pattern's own reference file.

2. **Cycle defaults**: Read scripts/orchestrator.py — find max_iterations and max_rounds defaults. Read each reference that uses cycles (chained-iteration.md, rag-grounded-refinement.md, rubric-based-refinement.md). Verify the defaults match. Also check scripts/status_tracking.py for its cycle defaults.

3. **Scoring constants**: Read skills/compose/scripts/score_debate_semantic.py — extract CHANGE_WEIGHT and SCORE_MAP. Read skills/compose/references/debate-panel.md — extract the formula description. Verify the constants implement the documented formula.

4. **Sample count defaults**: Verify the default sample count (3) is consistent across debate-panel.md, run_semantic_evals.py, and score_debate_semantic.py.

5. **Timeout values**: List all timeout constants across all Python files. Flag any that seem miscalibrated (e.g., a Haiku call getting a 30-minute timeout).

Write findings to review/04-numeric-constants.md.
```

---

## Check 5: Duplicated Code Drift

**Handoff file**: `review/05-duplicated-code.md`

**Prompt**:
```
You are auditing triplicated logic across the three main Python files for drift.

Three functions exist independently in multiple files. For each group, read the implementation in EVERY file and diff them:

1. **Agent path resolution**:
   - scripts/orchestrator.py: _resolve_agent_path
   - scripts/graph_monitor.py: _agent_path_candidates
   - scripts/status_tracking.py: _agent_path_candidates
   Do they search the same directories in the same order? If an agent file is at path X, would all three find it?

2. **Model label normalization**:
   - scripts/graph_monitor.py: _normalize_model_label
   - scripts/status_tracking.py: _normalize_model_label
   Are these byte-for-byte identical? Would a model string like "claude-sonnet-4-6" normalize the same way in both?

3. **Frontmatter parsing**:
   - scripts/orchestrator.py: _read_agent_frontmatter
   - scripts/graph_monitor.py: _extract_frontmatter_model
   - scripts/status_tracking.py: _read_frontmatter_model
   Given the same agent file, would all three extract the same model value? Test with edge cases: missing frontmatter, model with quotes, model with trailing spaces.

For each group, state whether they are consistent or have drifted. If drifted, document exactly what differs.

Write findings to review/05-duplicated-code.md.
```

---

## Check 6: State Value Contracts

**Handoff file**: `review/06-state-values.md`

**Prompt**:
```
You are auditing the state machine contracts between status writers and readers.

1. Read scripts/status_tracking.py — extract every string literal used as a node state (e.g., "pending", "running", "completed", "failed") and every string used as an overall run state (e.g., "idle", "running", "completed", "failed").

2. Read scripts/graph_monitor.py — extract every state string referenced in STATE_COLORS, state_colors, and any conditional checks on state values.

3. Read scripts/orchestrator.py — extract every call to set_node_state() and set_overall_state(). List the state string passed in each call.

Verify:
- Every state the orchestrator writes is handled by the GUI
- Every state the GUI maps to a color is actually written by something
- No typos or case mismatches between writers and readers
- The overall run lifecycle (idle -> running -> completed/failed) is consistent

Write findings to review/06-state-values.md.
```

---

## Check 7: File Path and Naming Conventions

**Handoff file**: `review/07-file-paths.md`

**Prompt**:
```
You are auditing file path conventions for consistency.

1. **Output file naming**: Read skills/compose/references/debate-panel.md — extract every output file path mentioned (e.g., output/{persona}-round{k}.md, output/final-selection.md, output/evaluations/eval-NNNN.csv). Then read run_semantic_evals.py and score_debate_semantic.py — verify the code writes to exactly those paths with exactly those naming patterns.

2. **Prompt file convention**: Read skills/compose/SKILL.md — find the prompt file naming convention. Read scripts/orchestrator.py — find _load_prompt(). Verify the convention matches.

3. **Agent file convention**: Read skills/compose/SKILL.md — find agent file naming. Read scripts/orchestrator.py — find _resolve_agent_path(). Verify they agree.

4. **Log file naming**: Read scripts/orchestrator.py — find every place log files are created. Are the naming patterns consistent (e.g., {name}.log for single runs, {name}-iter{i}.log for loops)?

5. **Status file path**: Read scripts/status_tracking.py — where does it write status.json? Read scripts/graph_monitor.py — where does it read status.json? Do they agree?

6. **Script copy convention**: debate-panel.md says "Copy both [scoring scripts] into the run directory at scaffold time." Read orchestrator.py's run_script() — does it find scripts at run_dir root? What's the fallback? Would a missing copy cause a silent failure or a clear error?

Write findings to review/07-file-paths.md.
```

---

## Check 8: Pattern Name Registry

**Handoff file**: `review/08-pattern-names.md`

**Prompt**:
```
You are auditing pattern name strings for consistency.

1. Read skills/compose/SKILL.md — extract every pattern name as it appears in the skill (both display names and any slug/identifier forms).

2. Read scripts/graph_monitor.py — find KNOWN_PATTERNS and every `pattern ==` or `pattern.startswith()` check in the layout logic. List every pattern string the GUI expects.

3. For each pattern, answer: if the compose skill writes `"pattern": "X"` in execution_plan.json, will graph_monitor.py's layout logic match it? Test exact matches and prefix matches.

4. Is KNOWN_PATTERNS actually used anywhere? If it's dead code, flag it.

5. Read scripts/orchestrator.py — does it reference pattern names anywhere? Does it need to?

Write findings to review/08-pattern-names.md.
```

---

## Check 9: Execution Plan Schema

**Handoff file**: `review/09-execution-plan-schema.md`

**Prompt**:
```
You are auditing the execution plan JSON schema for consistency between producers and consumers.

1. Read skills/compose/SKILL.md — extract the documented execution_plan.json schema (all fields, types, required vs. optional).

2. Read every reference file in skills/compose/references/ — extract every execution plan JSON template. List all fields used.

3. Read scripts/orchestrator.py — find every place it reads from the execution plan (node.get("X"), plan["X"], etc.). List every field accessed and whether it uses .get() with a default or direct access that would crash on missing keys.

4. Read scripts/graph_monitor.py — same analysis for plan reading.

Verify:
- Every field the orchestrator accesses is documented in SKILL.md
- Every field in the reference templates is accessed by the orchestrator
- No field uses direct access (plan["X"]) without a documented guarantee it exists
- The "node_type" field: which values exist? Are they all handled?
- The "cycle" field: is the schema documented? Do orchestrator, status_tracking, and graph_monitor all agree on its structure?

Write findings to review/09-execution-plan-schema.md.
```

---

## Check 10: Cross-Reference Sweep

**Handoff file**: `review/10-cross-reference-sweep.md`
**Depends on**: All previous handoff files (01-09)

**Prompt**:
```
You are performing the final cross-reference sweep. Read all files in the review/ directory (01 through 09). Then:

1. Compile a master list of every issue found across all checks.
2. Categorize each issue by severity:
   - CRITICAL: Will cause silent wrong behavior at runtime (e.g., wrong model used, writes silently fail)
   - HIGH: Will cause a visible error at runtime (e.g., crash, missing file)
   - MEDIUM: Inconsistency that doesn't affect current behavior but will break on future changes (e.g., duplicated code that has drifted, dead constants)
   - LOW: Documentation-only issues (e.g., stale comments, undocumented conventions)

3. For CRITICAL and HIGH issues, write a one-line fix description.
4. Check for any cross-check issues — problems that only become visible when combining findings from multiple checks (e.g., Check 1 says model is passed correctly but Check 5 says the frontmatter parser has drifted, meaning the model value itself might differ).

Write the consolidated report to review/10-cross-reference-sweep.md.
```

---

## Execution

Run checks 1-9 in parallel (they are independent). Run check 10 after all others complete.

Each check should take 2-5 minutes in an independent Claude Code context. Total wall-clock time: ~5 minutes parallel + ~3 minutes for the sweep.

To run a check, open a new Claude Code context in the plugin root directory and paste the prompt. The reviewer writes its findings to the handoff file. No reviewer modifies source code.
