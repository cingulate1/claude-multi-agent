#!/usr/bin/env python3
"""Invoke Haiku agents to evaluate pairwise semantic similarity for debate-panel scoring.

For each round transition, for each panelist, asks two questions:
  Q1: To what extent did this panelist change their answer? (1-5)
  Q2: To what extent did each other panelist move toward this panelist's answer? (1-5)

Each evaluation is an independent Haiku invocation with no knowledge of the
larger workflow. Samples cycle through three prompt variants to decorrelate
repeated evaluations:
  Variant 0: Standard 1-5 numeric scale, Respondent A/B labels
  Variant 1: A-E letter scale, Respondent 1/2 labels
  Variant 2: Reversed 5-1 numeric scale, Respondent A/B labels
All responses are normalized back to canonical 1-5 before writing.

Writes one CSV file per evaluation to output/evaluations/eval-NNNN.csv,
each containing a single integer 1-5.

Concurrency: Evaluations are dispatched via asyncio with a rolling-window
strategy. An initial burst of `--max-concurrent` is fired, then each time
`--refill-size` evaluations complete, the same number is dispatched until
all are queued. `--max-concurrent` defaults to a value auto-sized from
available system RAM (each `claude -p` Node process peaks ~600 MB RSS).

Usage:
    cd <run_dir> && python run_semantic_evals.py
    cd <run_dir> && python run_semantic_evals.py --samples 5
    cd <run_dir> && python run_semantic_evals.py --max-concurrent 16 --refill-size 4
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import logging
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROUND_FILE_PATTERN = re.compile(r"^(.+)-round(\d+)\.md$")
HAIKU_TIMEOUT = 120  # 2 minutes per evaluation — these are tiny

log = logging.getLogger("run_semantic_evals")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def _agent_env():
    """Return an environment dict safe for spawning nested claude processes."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


# ---------------------------------------------------------------------------
# RAM detection (cross-platform, stdlib-only)
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]


def _windows_mem_status():
    m = _MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m


def available_ram_gb() -> float:
    """Currently-available RAM in GB. Includes free + reclaimable cache on Windows."""
    if sys.platform == "win32":
        return _windows_mem_status().ullAvailPhys / (1024 ** 3)
    if sys.platform == "linux":
        try:
            avail = free = None
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        avail = int(line.split()[1]) / (1024 ** 2)
                    elif line.startswith("MemFree:"):
                        free = int(line.split()[1]) / (1024 ** 2)
            return avail if avail is not None else (free or 4.0)
        except OSError:
            pass
    return 4.0  # conservative fallback for unknown platforms


def auto_max_concurrent(
    per_worker_mb: int = 600,
    headroom_gb: float = 4.0,
    hard_max: int = 32,
    hard_min: int = 1,
) -> int:
    """Pick a safe concurrency cap based on currently-available RAM.

    Each `claude -p` Node process peaks at ~400-600 MB RSS during inference.
    `headroom_gb` is left for the OS, parent Python, browsers, etc.
    `hard_max` clamps the upper bound regardless of RAM (Windows
    CreateProcess + Defender start serializing past ~32 simultaneous spawns).
    """
    avail = available_ram_gb()
    by_ram = int((avail - headroom_gb) * 1024 / per_worker_mb)
    return max(hard_min, min(hard_max, by_ram))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_panelists(output_dir: Path) -> tuple[list[str], int]:
    """Discover panelist names and round count from output files."""
    roster: dict[str, set[int]] = defaultdict(set)
    for entry in sorted(output_dir.iterdir()):
        if not entry.is_file():
            continue
        match = ROUND_FILE_PATTERN.match(entry.name)
        if not match:
            continue
        roster[match.group(1)].add(int(match.group(2)))

    if not roster:
        raise RuntimeError("No panelist round files found")

    panelists = sorted(roster.keys())
    num_rounds = max(r for rounds in roster.values() for r in rounds) + 1
    return panelists, num_rounds


def read_final_answer(file_path: Path) -> str:
    """Read a panelist output file and extract the ## Final Answer section."""
    content = file_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    heading_re = re.compile(r"^##\s+Final\s+Answer\s*$", re.IGNORECASE)

    heading_idx = None
    for i, line in enumerate(lines):
        if heading_re.match(line.strip()):
            heading_idx = i

    if heading_idx is not None:
        return "\n".join(lines[heading_idx + 1:]).strip()
    # Fall back to full content if no heading found
    return content.strip()


# ---------------------------------------------------------------------------
# Task ID generation
# ---------------------------------------------------------------------------

def task_id_for(eval_index: int) -> str:
    """Generate a deterministic task ID from the 4-digit evaluation index."""
    padded = f"{eval_index:04d}"
    return hashlib.sha256(padded.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _prompt_variant(sample: int) -> int:
    """Return the prompt variant (0, 1, or 2) for a given sample index."""
    return sample % 3


def build_q1_prompt(
    task_id: str,
    panelist: str,
    answer_before: str,
    answer_after: str,
    variant: int = 0,
) -> str:
    """Build the Q1 prompt: how much did this panelist change their answer?"""
    if variant == 1:
        scale = (
            "A = No change at all — the same recommendation/conclusion, possibly reworded\n"
            "B = Minor refinement — same core position with small additions or clarifications\n"
            "C = Moderate shift — the core recommendation is recognizably similar but with significant modifications\n"
            "D = Major change — the conclusion has substantially shifted, though some elements remain\n"
            "E = Complete reversal — an entirely different position\n\n"
            "Respond with a single letter from A to E. Nothing else."
        )
    elif variant == 2:
        scale = (
            "5 = No change at all — the same recommendation/conclusion, possibly reworded\n"
            "4 = Minor refinement — same core position with small additions or clarifications\n"
            "3 = Moderate shift — the core recommendation is recognizably similar but with significant modifications\n"
            "2 = Major change — the conclusion has substantially shifted, though some elements remain\n"
            "1 = Complete reversal — an entirely different position\n\n"
            "Respond with a single integer from 1 to 5. Nothing else."
        )
    else:
        scale = (
            "1 = No change at all — the same recommendation/conclusion, possibly reworded\n"
            "2 = Minor refinement — same core position with small additions or clarifications\n"
            "3 = Moderate shift — the core recommendation is recognizably similar but with significant modifications\n"
            "4 = Major change — the conclusion has substantially shifted, though some elements remain\n"
            "5 = Complete reversal — an entirely different position\n\n"
            "Respond with a single integer from 1 to 5. Nothing else."
        )

    return f"""<taskID>{task_id}</taskID>

You are evaluating how much a respondent changed their position between two rounds of a discussion.

Here is the respondent's answer in the earlier round:

<answer_before>
{answer_before}
</answer_before>

Here is the respondent's answer in the later round:

<answer_after>
{answer_after}
</answer_after>

To what extent did the respondent change their core position?

{scale}"""


def build_q2_prompt(
    task_id: str,
    panelist: str,
    other: str,
    other_answer_before: str,
    other_answer_after: str,
    panelist_answer: str,
    variant: int = 0,
) -> str:
    """Build a Q2 prompt: how much did the other panelist move toward this panelist?"""
    if variant == 1:
        ref_label, other_label = "Respondent 1", "Respondent 2"
        ref_tag, other_before_tag, other_after_tag = "respondent_1", "respondent_2_before", "respondent_2_after"
        scale = (
            "A = No movement toward 1 — 2's position is equally or more distant from 1\n"
            "B = Slight movement — 2 adopted minor elements of 1's position\n"
            "C = Moderate convergence — 2's new position shares significant common ground with 1\n"
            "D = Strong convergence — 2's new position is closely aligned with 1\n"
            "E = Full adoption — 2 essentially adopted 1's position\n\n"
            "Respond with a single letter from A to E. Nothing else."
        )
    elif variant == 2:
        ref_label, other_label = "Respondent A", "Respondent B"
        ref_tag, other_before_tag, other_after_tag = "respondent_a", "respondent_b_before", "respondent_b_after"
        scale = (
            "5 = No movement toward A — B's position is equally or more distant from A\n"
            "4 = Slight movement — B adopted minor elements of A's position\n"
            "3 = Moderate convergence — B's new position shares significant common ground with A\n"
            "2 = Strong convergence — B's new position is closely aligned with A\n"
            "1 = Full adoption — B essentially adopted A's position\n\n"
            "Respond with a single integer from 1 to 5. Nothing else."
        )
    else:
        ref_label, other_label = "Respondent A", "Respondent B"
        ref_tag, other_before_tag, other_after_tag = "respondent_a", "respondent_b_before", "respondent_b_after"
        scale = (
            "1 = No movement toward A — B's position is equally or more distant from A\n"
            "2 = Slight movement — B adopted minor elements of A's position\n"
            "3 = Moderate convergence — B's new position shares significant common ground with A\n"
            "4 = Strong convergence — B's new position is closely aligned with A\n"
            "5 = Full adoption — B essentially adopted A's position\n\n"
            "Respond with a single integer from 1 to 5. Nothing else."
        )

    return f"""<taskID>{task_id}</taskID>

You are evaluating whether one respondent's position moved closer to another respondent's position between two rounds of a discussion.

Here is {ref_label}'s position (the reference position):

<{ref_tag}>
{panelist_answer}
</{ref_tag}>

Here is {other_label}'s position in the earlier round:

<{other_before_tag}>
{other_answer_before}
</{other_before_tag}>

Here is {other_label}'s position in the later round:

<{other_after_tag}>
{other_answer_after}
</{other_after_tag}>

To what extent did {other_label} move toward {ref_label}'s position?

{scale}"""


# ---------------------------------------------------------------------------
# Score parsing
# ---------------------------------------------------------------------------

_LETTER_MAP = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5,
               "a": 1, "b": 2, "c": 3, "d": 4, "e": 5}


def parse_score(raw: str, eval_index: int, variant: int = 0) -> int:
    """Extract a 1-5 canonical score from Haiku's response.

    Variant 0: 1-5 numeric, ascending (no transform)
    Variant 1: A-E letters (map to 1-5)
    Variant 2: 5-1 numeric, reversed (invert: canonical = 6 - raw)
    """
    if variant == 1:
        for char in raw:
            if char in _LETTER_MAP:
                return _LETTER_MAP[char]
        # Fall back to numeric in case model ignored the letter instruction
        for char in raw:
            if char in "12345":
                return int(char)
    else:
        for char in raw:
            if char in "12345":
                score = int(char)
                return (6 - score) if variant == 2 else score

    raise RuntimeError(
        f"Evaluation {eval_index:04d}: could not parse score from Haiku response: '{raw[:100]}'"
    )


# ---------------------------------------------------------------------------
# Async Haiku invocation
# ---------------------------------------------------------------------------

async def invoke_haiku_async(prompt: str, timeout: int = HAIKU_TIMEOUT) -> str:
    """Run a single Haiku evaluation asynchronously and return the raw stdout text."""
    cmd = [
        "claude",
        "-p", prompt,
        "--model", "haiku",
        "--output-format", "text",
        "--no-session-persistence",
        "--max-turns", "1",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_agent_env(),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        raise RuntimeError(f"Haiku invocation timed out after {timeout}s")

    if proc.returncode != 0:
        stderr = stderr_b.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Haiku exited with code {proc.returncode}: "
            f"{stderr[:200] if stderr else '(no stderr)'}"
        )

    return stdout_b.decode("utf-8", errors="replace").strip()


# ---------------------------------------------------------------------------
# Eval list construction
# ---------------------------------------------------------------------------

def build_evals(
    panelists: list[str],
    num_rounds: int,
    n_samples: int,
    output_dir: Path,
) -> list[dict]:
    """Build the full ordered eval list with prompts pre-rendered."""
    answers: dict[str, dict[int, str]] = {}
    for p in panelists:
        answers[p] = {}
        for k in range(num_rounds):
            f = output_dir / f"{p}-round{k}.md"
            if not f.is_file():
                raise RuntimeError(f"Missing output file: {f.name}")
            answers[p][k] = read_final_answer(f)

    n_transitions = num_rounds - 1
    evals: list[dict] = []
    eval_index = 0

    for t in range(n_transitions):
        round_from = t
        round_to = t + 1

        for p_idx, panelist in enumerate(panelists):
            answer_before = answers[panelist][round_from]
            answer_after = answers[panelist][round_to]
            others = [p for i, p in enumerate(panelists) if i != p_idx]

            # Q1: how much did this panelist change?
            for sample in range(n_samples):
                eval_index += 1
                variant = _prompt_variant(sample)
                tid = task_id_for(eval_index)
                prompt = build_q1_prompt(tid, panelist, answer_before, answer_after, variant)
                evals.append({
                    "eval_index": eval_index,
                    "variant": variant,
                    "summary": (
                        f"Q1 {panelist} r{round_from}->r{round_to} "
                        f"sample {sample + 1}/{n_samples} v{variant}"
                    ),
                    "prompt": prompt,
                })

            # Q2: for each other panelist, how much did they move toward this panelist?
            for other in others:
                other_before = answers[other][round_from]
                other_after = answers[other][round_to]
                panelist_ref = answers[panelist][round_from]

                for sample in range(n_samples):
                    eval_index += 1
                    variant = _prompt_variant(sample)
                    tid = task_id_for(eval_index)
                    prompt = build_q2_prompt(
                        tid, panelist, other,
                        other_before, other_after, panelist_ref,
                        variant,
                    )
                    evals.append({
                        "eval_index": eval_index,
                        "variant": variant,
                        "summary": (
                            f"Q2 {other}->toward {panelist} r{round_from}->r{round_to} "
                            f"sample {sample + 1}/{n_samples} v{variant}"
                        ),
                        "prompt": prompt,
                    })

    return evals


# ---------------------------------------------------------------------------
# Per-eval coroutine
# ---------------------------------------------------------------------------

async def evaluate_one(eval_meta: dict, eval_dir: Path, timeout: int) -> tuple[int, int]:
    """Invoke Haiku for one eval, parse the score, write the CSV. Returns (idx, score)."""
    eval_index = eval_meta["eval_index"]
    variant = eval_meta["variant"]
    csv_path = eval_dir / f"eval-{eval_index:04d}.csv"

    log.info(f"  eval-{eval_index:04d} {eval_meta['summary']}")
    raw = await invoke_haiku_async(eval_meta["prompt"], timeout)
    score = parse_score(raw, eval_index, variant)

    csv_path.write_text(str(score), encoding="utf-8")
    log.info(f"  eval-{eval_index:04d} -> {score}")
    return (eval_index, score)


# ---------------------------------------------------------------------------
# Rolling-window dispatch
# ---------------------------------------------------------------------------

async def run_evaluations(
    run_dir: Path,
    panelists: list[str],
    num_rounds: int,
    n_samples: int,
    max_concurrent: int,
    refill_size: int,
    timeout: int,
) -> list[tuple[int, int]]:
    """Build the eval list and dispatch via rolling-window async.

    Returns list of (eval_index, score) tuples in eval_index order.
    """
    output_dir = run_dir / "output"
    eval_dir = run_dir / "output" / "evaluations"
    eval_dir.mkdir(parents=True, exist_ok=True)

    evals = build_evals(panelists, num_rounds, n_samples, output_dir)

    # Pre-pass: separate cached (CSV exists) from uncached
    results: list[tuple[int, int]] = []
    uncached: list[dict] = []
    for e in evals:
        csv_path = eval_dir / f"eval-{e['eval_index']:04d}.csv"
        if csv_path.is_file():
            existing = int(csv_path.read_text(encoding="utf-8").strip())
            results.append((e["eval_index"], existing))
            log.info(f"  eval-{e['eval_index']:04d} [cached] {e['summary']}: {existing}")
        else:
            uncached.append(e)

    if not uncached:
        log.info(f"All {len(evals)} evaluations already cached")
        return sorted(results)

    n = len(uncached)
    log.info(
        f"Dispatching {n} uncached evals "
        f"(max_concurrent={max_concurrent}, refill_size={refill_size})"
    )

    pending: set[asyncio.Task] = set()
    next_idx = 0
    initial = min(max_concurrent, n)
    log.info(f"  Initial burst: dispatching {initial} concurrent")
    for _ in range(initial):
        task = asyncio.create_task(evaluate_one(uncached[next_idx], eval_dir, timeout))
        pending.add(task)
        next_idx += 1

    slots_freed = 0
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                idx, score = task.result()  # raises if the coroutine raised
                results.append((idx, score))
                slots_freed += 1

            while slots_freed >= refill_size and next_idx < n:
                chunk = min(refill_size, n - next_idx)
                inflight_after = len(pending) + chunk
                log.info(
                    f"  Refill: dispatching {chunk} more "
                    f"(running total: {next_idx + chunk}/{n}, inflight ~{inflight_after})"
                )
                for _ in range(chunk):
                    task = asyncio.create_task(evaluate_one(uncached[next_idx], eval_dir, timeout))
                    pending.add(task)
                    next_idx += 1
                slots_freed -= chunk
    except Exception:
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        raise

    log.info(f"All {len(evals)} evaluations complete")
    return sorted(results)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def amain(args: argparse.Namespace) -> int:
    run_dir = Path.cwd()
    if not run_dir.is_dir():
        log.error(f"Run directory does not exist: {run_dir}")
        return 1

    output_dir = run_dir / "output"
    if not output_dir.is_dir():
        log.error(f"Output directory does not exist: {output_dir}")
        return 1

    try:
        panelists, num_rounds = discover_panelists(output_dir)
    except RuntimeError as e:
        log.error(str(e))
        return 1

    n_transitions = num_rounds - 1
    n_panelists = len(panelists)
    questions_per_element = 1 + (n_panelists - 1)
    total_evals = n_transitions * n_panelists * questions_per_element * args.samples

    # Resolve concurrency settings
    max_concurrent = args.max_concurrent if args.max_concurrent > 0 else auto_max_concurrent()
    refill_size = args.refill_size if args.refill_size > 0 else max(1, max_concurrent // 4)

    log.info(f"Panelists: {n_panelists} ({', '.join(panelists)})")
    log.info(f"Rounds: {num_rounds}, transitions: {n_transitions}")
    log.info(f"Samples: {args.samples}")
    log.info(f"Total evaluations: {total_evals}")
    log.info(
        f"Concurrency: max_concurrent={max_concurrent}"
        f"{' (auto)' if args.max_concurrent <= 0 else ''}, "
        f"refill_size={refill_size}"
        f"{' (auto)' if args.refill_size <= 0 else ''}, "
        f"available_ram={available_ram_gb():.1f} GB"
    )

    try:
        await run_evaluations(
            run_dir, panelists, num_rounds, args.samples,
            max_concurrent, refill_size, args.timeout,
        )
    except RuntimeError as e:
        log.error(f"Evaluation failed: {e}")
        return 1

    log.info("Done. Run score_debate_semantic.py to compute final scores.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Haiku semantic evaluations for debate-panel scoring"
    )
    parser.add_argument(
        "--samples", type=int, default=3,
        help="Samples per evaluation (default: 3)",
    )
    parser.add_argument(
        "--max-concurrent", type=int, default=0,
        help="Max simultaneous Haiku subprocesses (default: auto-sized from available RAM, capped at 32)",
    )
    parser.add_argument(
        "--refill-size", type=int, default=0,
        help="Rolling-window refill chunk size (default: max-concurrent // 4, min 1)",
    )
    parser.add_argument(
        "--timeout", type=int, default=HAIKU_TIMEOUT,
        help=f"Per-Haiku subprocess timeout in seconds (default: {HAIKU_TIMEOUT})",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        log.warning("Interrupted by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
