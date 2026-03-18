#!/usr/bin/env python3
"""Semantic Free-MAD scoring for the debate-panel pattern.

Reads evaluation CSV files produced by the Haiku similarity agent,
builds a coefficient matrix, applies the adapted Free-MAD scoring
formula with round decay, and writes the winning panelist's answer
to output/final-selection.md.

Each CSV file contains a single integer from 1-5.

File ordering (sequential, 1-indexed):
  For each transition (round 0->1, then 1->2, ...):
    For each panelist (sorted alphabetically):
      Q1 x nSamples files       (how much did this panelist change?)
      Q2_other1 x nSamples files (how much did other1 move toward this panelist?)
      Q2_other2 x nSamples files
      Q2_other3 x nSamples files
      ...

Score mapping: 1=0.00, 2=0.25, 3=0.50, 4=0.75, 5=1.00

Formula per element (panelist P, transition k-1 -> k):
    change    = Q1* x -1 x 1.5
    converge  = sum(Q2*_j for each other panelist j)
    element   = (change + converge) x f
    where f   = 1 / (k + 1)

The 1.5 multiplier preserves (w2+w4)/w3 = (25+20)/30 from Free-MAD.

Usage:
    python score_debate_semantic.py <run_dir>
    python score_debate_semantic.py <run_dir> --samples 5
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCORE_MAP = {1: 0.00, 2: 0.25, 3: 0.50, 4: 0.75, 5: 1.00}
CHANGE_WEIGHT = 1.5  # (w2 + w4) / w3 = 45 / 30

OUTPUT_DIR = "output"
EVAL_DIR = "output/evaluations"
FINAL_SELECTION_FILE = "output/final-selection.md"
SCORE_DETAILS_FILE = "output/score-details.json"

ROUND_FILE_PATTERN = re.compile(r"^(.+)-round(\d+)\.md$")
FINAL_ANSWER_HEADING = re.compile(r"^##\s+Final\s+Answer\s*$", re.IGNORECASE)

log = logging.getLogger("score_debate_semantic")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_panelists(output_dir: Path) -> tuple[list[str], int]:
    """Discover panelist names and round count from output files.

    Returns (sorted panelist names, total number of rounds).
    """
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


# ---------------------------------------------------------------------------
# Evaluation file reading
# ---------------------------------------------------------------------------

def read_eval_files(
    eval_dir: Path,
    n_panelists: int,
    n_transitions: int,
    n_samples: int,
) -> list[int]:
    """Read all evaluation CSV files in sequential order.

    Returns a flat list of integer scores (1-5).
    """
    questions_per_element = 1 + (n_panelists - 1)
    total_files = n_transitions * n_panelists * questions_per_element * n_samples

    scores: list[int] = []
    for i in range(1, total_files + 1):
        candidates = [
            eval_dir / f"eval-{i:04d}.csv",
            eval_dir / f"eval-{i:03d}.csv",
            eval_dir / f"eval_{i:04d}.csv",
            eval_dir / f"eval_{i:03d}.csv",
            eval_dir / f"{i:04d}.csv",
            eval_dir / f"{i:03d}.csv",
            eval_dir / f"{i}.csv",
        ]

        found = None
        for c in candidates:
            if c.is_file():
                found = c
                break

        if found is None:
            raise RuntimeError(
                f"Evaluation file #{i} of {total_files} not found in {eval_dir}. "
                f"Tried: {', '.join(c.name for c in candidates)}"
            )

        text = found.read_text(encoding="utf-8").strip()
        try:
            value = int(text)
        except ValueError:
            raise RuntimeError(
                f"Invalid score in {found.name}: expected integer 1-5, got '{text}'"
            )

        if value < 1 or value > 5:
            raise RuntimeError(
                f"Score out of range in {found.name}: expected 1-5, got {value}"
            )

        scores.append(value)

    log.info(f"Read {len(scores)} evaluation files")
    return scores


# ---------------------------------------------------------------------------
# Coefficient matrix
# ---------------------------------------------------------------------------

def build_coefficient_matrix(
    scores: list[int],
    panelists: list[str],
    n_transitions: int,
    n_samples: int,
) -> dict[str, list[dict]]:
    """Parse flat score list into structured coefficient matrix.

    Returns:
        {panelist: [
            {
                "transition": (from_round, to_round),
                "q1": float,           # averaged, mapped change score
                "q2": {other: float},  # averaged, mapped convergence scores
                "q1_raw": [int, ...],  # raw sample values for audit
                "q2_raw": {other: [int, ...]},
            },
            ...
        ]}
    """
    idx = 0
    matrix: dict[str, list[dict]] = {p: [] for p in panelists}

    for t in range(n_transitions):
        round_from = t
        round_to = t + 1

        for p_idx, panelist in enumerate(panelists):
            # Q1 samples
            q1_raw = scores[idx:idx + n_samples]
            idx += n_samples
            q1_avg = sum(SCORE_MAP[v] for v in q1_raw) / n_samples

            # Q2 samples for each other panelist (sorted order)
            others = [p for i, p in enumerate(panelists) if i != p_idx]
            q2_avgs: dict[str, float] = {}
            q2_raws: dict[str, list[int]] = {}

            for other in others:
                q2_raw = scores[idx:idx + n_samples]
                idx += n_samples
                q2_avgs[other] = sum(SCORE_MAP[v] for v in q2_raw) / n_samples
                q2_raws[other] = q2_raw

            matrix[panelist].append({
                "transition": (round_from, round_to),
                "q1": q1_avg,
                "q2": q2_avgs,
                "q1_raw": q1_raw,
                "q2_raw": q2_raws,
            })

    return matrix


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def compute_scores(
    matrix: dict[str, list[dict]],
    panelists: list[str],
) -> tuple[dict[str, float], list[dict]]:
    """Compute final scores per panelist.

    Formula per element:
        change_component    = Q1* x -1 x 1.5
        convergence_component = sum(Q2*_j)
        element_score       = (change + convergence) x f
        where f = 1 / (round_to + 1)

    Returns (scores dict, events list for audit).
    """
    scores: dict[str, float] = {}
    events: list[dict] = []

    for panelist in panelists:
        total = 0.0

        for entry in matrix[panelist]:
            round_to = entry["transition"][1]
            f = 1.0 / (round_to + 1)

            q1 = entry["q1"]
            q2_sum = sum(entry["q2"].values())

            change = (q1 * -1) * CHANGE_WEIGHT
            convergence = q2_sum
            element = (change + convergence) * f

            total += element

            events.append({
                "panelist": panelist,
                "transition": f"round{entry['transition'][0]}->round{entry['transition'][1]}",
                "decay_factor": round(f, 4),
                "q1_avg": round(q1, 4),
                "q1_raw": entry["q1_raw"],
                "q2_avgs": {k: round(v, 4) for k, v in entry["q2"].items()},
                "q2_raw": {k: v for k, v in entry["q2_raw"].items()},
                "change_component": round(change, 4),
                "convergence_component": round(convergence, 4),
                "element_score_undecayed": round(change + convergence, 4),
                "element_score": round(element, 4),
            })

        scores[panelist] = round(total, 4)

    return scores, events


def select_winner(scores: dict[str, float]) -> tuple[str, float]:
    """Select winning panelist. Ties broken randomly."""
    if not scores:
        raise RuntimeError("No scores computed")
    max_score = max(scores.values())
    tied = [p for p, s in scores.items() if s == max_score]
    return random.choice(tied), max_score


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def extract_final_answer(file_path: Path) -> str:
    """Extract text after the last '## Final Answer' heading."""
    content = file_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    heading_idx = None
    for i, line in enumerate(lines):
        if FINAL_ANSWER_HEADING.match(line.strip()):
            heading_idx = i

    if heading_idx is not None:
        return "\n".join(lines[heading_idx + 1:]).strip()
    return content.strip()


def write_final_selection(
    run_dir: Path,
    scores: dict[str, float],
    winner: str,
    winner_score: float,
    panelists: list[str],
    num_rounds: int,
) -> Path:
    """Write output/final-selection.md."""
    answer_file = run_dir / OUTPUT_DIR / f"{winner}-round{num_rounds - 1}.md"
    if not answer_file.is_file():
        raise RuntimeError(f"Winner's answer file not found: {answer_file}")

    verbatim = extract_final_answer(answer_file)
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    lines = [
        "# Debate Panel Result (Semantic Scoring)",
        "",
        "## Scoring Summary",
        "",
        f"- **Panelists:** {len(panelists)} ({', '.join(panelists)})",
        f"- **Rounds:** {num_rounds} (0 through {num_rounds - 1})",
        f"- **Method:** Semantic Free-MAD (Haiku-evaluated similarity coefficients)",
        f"- **Change weight:** {CHANGE_WEIGHT} ((w2+w4)/w3 = 45/30)",
        "",
        "| Rank | Panelist | Score |",
        "|------|----------|-------|",
    ]

    for rank, (p, s) in enumerate(sorted_scores, 1):
        marker = " **(winner)**" if p == winner else ""
        lines.append(f"| {rank} | {p}{marker} | {s:+.4f} |")

    lines.extend(["", "## Final Answer", "", verbatim, ""])

    out_path = run_dir / FINAL_SELECTION_FILE
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def write_score_details(
    run_dir: Path,
    scores: dict[str, float],
    events: list[dict],
    winner: str,
    winner_score: float,
    panelists: list[str],
    num_rounds: int,
    n_samples: int,
) -> Path:
    """Write output/score-details.json."""
    detail = {
        "algorithm": "semantic-free-mad",
        "change_weight": CHANGE_WEIGHT,
        "score_map": {str(k): v for k, v in SCORE_MAP.items()},
        "samples_per_evaluation": n_samples,
        "panelists": panelists,
        "num_rounds": num_rounds,
        "scores": scores,
        "winner": {"panelist": winner, "score": winner_score},
        "events": events,
    }

    out_path = run_dir / SCORE_DETAILS_FILE
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(detail, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Semantic Free-MAD scoring for debate-panel runs"
    )
    parser.add_argument("run_dir", help="Path to the run directory")
    parser.add_argument(
        "--samples", type=int, default=3,
        help="Number of samples per evaluation (default: 3)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        log.error(f"Run directory does not exist: {run_dir}")
        return 1

    output_dir = run_dir / OUTPUT_DIR
    eval_dir = run_dir / EVAL_DIR

    if not output_dir.is_dir():
        log.error(f"Output directory does not exist: {output_dir}")
        return 1
    if not eval_dir.is_dir():
        log.error(f"Evaluations directory does not exist: {eval_dir}")
        return 1

    # Discover panelists
    try:
        panelists, num_rounds = discover_panelists(output_dir)
    except RuntimeError as e:
        log.error(str(e))
        return 1

    n_transitions = num_rounds - 1
    n_panelists = len(panelists)
    expected_files = n_transitions * n_panelists * (1 + (n_panelists - 1)) * args.samples

    log.info(f"Panelists: {n_panelists} ({', '.join(panelists)})")
    log.info(f"Rounds: {num_rounds}, transitions: {n_transitions}")
    log.info(f"Samples per evaluation: {args.samples}")
    log.info(f"Expected evaluation files: {expected_files}")

    # Read evaluations
    try:
        raw_scores = read_eval_files(eval_dir, n_panelists, n_transitions, args.samples)
    except RuntimeError as e:
        log.error(str(e))
        return 1

    # Build matrix
    matrix = build_coefficient_matrix(raw_scores, panelists, n_transitions, args.samples)

    # Score
    scores, events = compute_scores(matrix, panelists)

    log.info("Final scores:")
    for p, s in sorted(scores.items(), key=lambda x: x[1], reverse=True):
        log.info(f"  {s:+.4f}  {p}")

    # Winner
    winner, winner_score = select_winner(scores)
    log.info(f"Winner: {winner} ({winner_score:+.4f})")

    # Write
    try:
        sel = write_final_selection(
            run_dir, scores, winner, winner_score, panelists, num_rounds,
        )
        log.info(f"Wrote {sel}")

        det = write_score_details(
            run_dir, scores, events, winner, winner_score,
            panelists, num_rounds, args.samples,
        )
        log.info(f"Wrote {det}")
    except (OSError, RuntimeError) as e:
        log.error(f"Failed to write output: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
