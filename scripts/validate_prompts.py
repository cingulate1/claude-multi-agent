#!/usr/bin/env python3
"""Validate that each agent prompt contains its expected output path.

Reads execution_plan.json to get expected outputs per node, then checks
that each agent's prompt file contains the absolute output path in its
final lines. Reports any prompts that fail validation.

Usage:
    python validate_prompts.py --plan PATH/TO/execution_plan.json
"""

import argparse
import json
import sys
from pathlib import Path


def validate(plan_path: Path) -> list[dict]:
    """Returns a list of failure dicts, empty if all pass."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    run_dir = Path(plan["run_dir"])
    failures = []

    for node in plan.get("nodes", []):
        name = node["name"]
        outputs = node.get("outputs", [])
        if not outputs:
            continue

        prompt_file = run_dir / "agents" / f"{name}-prompt.txt"
        if not prompt_file.is_file():
            failures.append({"agent": name, "reason": "prompt file missing"})
            continue

        prompt_text = prompt_file.read_text(encoding="utf-8")

        # Check last 500 chars for the expected output path
        tail = prompt_text[-500:]

        for output_rel in outputs:
            # Resolve the absolute path, normalize separators
            abs_path = str((run_dir / output_rel).resolve())
            # Also check with forward slashes (Claude may use either)
            abs_path_fwd = abs_path.replace("\\", "/")

            if abs_path not in tail and abs_path_fwd not in tail:
                failures.append({
                    "agent": name,
                    "reason": f"output path not found in final lines",
                    "expected": abs_path_fwd,
                    "prompt_tail": tail.strip()[-200:],
                })

    return failures


def main():
    parser = argparse.ArgumentParser(description="Validate agent prompts")
    parser.add_argument("--plan", required=True, help="Path to execution_plan.json")
    args = parser.parse_args()

    plan_path = Path(args.plan)
    if not plan_path.is_file():
        print(f"ERROR: Plan file not found: {plan_path}", file=sys.stderr)
        sys.exit(1)

    failures = validate(plan_path)

    if not failures:
        print("OK: All prompts contain their expected output paths.")
        sys.exit(0)

    print(f"FAILED: {len(failures)} prompt(s) missing output path instructions:\n")
    for f in failures:
        print(f"  {f['agent']}:")
        print(f"    {f['reason']}")
        if "expected" in f:
            print(f"    expected: {f['expected']}")
            print(f"    prompt ends with: ...{f['prompt_tail']}")
        print()

    sys.exit(1)


if __name__ == "__main__":
    main()
