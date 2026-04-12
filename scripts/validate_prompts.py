#!/usr/bin/env python3
"""Validate that each agent prompt ends with a well-formed output instruction.

Reads execution_plan.json to get expected outputs per node, then checks
that each agent's prompt file ends with a line matching:
    Write your output to <path>
where <path> matches the expected absolute output path from the plan.

Usage:
    python validate_prompts.py --plan PATH/TO/execution_plan.json
"""

import argparse
import json
import re
import sys
from pathlib import Path

OUTPUT_LINE_RE = re.compile(r"^Write your output to .+$")


def validate_all(plan_path: Path) -> tuple[bool, list[str]]:
    """Validate all agent prompts against the execution plan.

    Returns (success, errors) where success is True if all prompts pass,
    and errors is a list of human-readable error strings.
    """
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    run_dir = Path(plan["run_dir"])
    errors = []

    for node in plan.get("nodes", []):
        name = node["name"]
        node_type = node.get("node_type", "agent")
        outputs = node.get("outputs", [])

        if not outputs:
            errors.append(f"{name}: node declares no outputs")
            continue

        # Script nodes have no prompt files — only validate outputs exist
        if node_type == "script":
            continue

        prompt_file = run_dir / "agents" / f"{name}-prompt.txt"
        if not prompt_file.is_file():
            errors.append(f"{name}: prompt file missing")
            continue

        prompt_text = prompt_file.read_text(encoding="utf-8")

        # Find the last non-empty line
        last_line = ""
        for line in reversed(prompt_text.splitlines()):
            if line.strip():
                last_line = line.strip()
                break

        if not OUTPUT_LINE_RE.match(last_line):
            errors.append(
                f"{name}: last line does not match expected format "
                f"(got: {last_line!r})"
            )
            continue

        # Extract the path from the line and verify it matches expected output
        path_in_line = last_line[len("Write your output to "):].strip()

        for output_rel in outputs:
            abs_path = str((run_dir / output_rel).resolve())
            abs_path_fwd = abs_path.replace("\\", "/")
            if path_in_line != abs_path and path_in_line != abs_path_fwd:
                errors.append(
                    f"{name}: output path mismatch — prompt says {path_in_line!r}, "
                    f"plan expects {abs_path_fwd!r}"
                )

    return (len(errors) == 0, errors)


def main():
    parser = argparse.ArgumentParser(description="Validate agent prompts")
    parser.add_argument("--plan", required=True, help="Path to execution_plan.json")
    args = parser.parse_args()

    plan_path = Path(args.plan)
    if not plan_path.is_file():
        print(f"ERROR: Plan file not found: {plan_path}", file=sys.stderr)
        sys.exit(1)

    success, errors = validate_all(plan_path)

    if success:
        print("OK: All prompts contain valid output instructions.")
        sys.exit(0)

    print(f"FAILED: {len(errors)} prompt(s) with invalid output instructions:\n")
    for err in errors:
        print(f"  {err}")
    print()

    sys.exit(1)


if __name__ == "__main__":
    main()
