#!/usr/bin/env python3
"""Create a subagent .md file from the subagent.md template.

Usage:
    python create-subagent.py <name> <description> <tools> <model> <output_dir> [effort]

Copies the subagent.md asset to <output_dir>/{name}.md,
filling in lines 2-6 of the frontmatter with the provided arguments.
The effort argument is optional; if omitted the effort field is left blank.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ASSET_PATH = SKILL_DIR / "assets" / "subagent.md"


def main():
    if len(sys.argv) < 6 or len(sys.argv) > 7:
        print(f"Usage: {sys.argv[0]} <name> <description> <tools> <model> <output_dir> [effort]",
              file=sys.stderr)
        sys.exit(1)

    name, description, tools, model, output_dir = sys.argv[1:6]
    effort = sys.argv[6] if len(sys.argv) == 7 else ""

    with open(ASSET_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Lines 2-6 (0-indexed 1-5) are the frontmatter fields to fill
    lines[1] = f"name: {name}\n"
    lines[2] = f"description: {description}\n"
    lines[3] = f"tools: {tools}\n"
    lines[4] = f"model: {model}\n"
    lines[5] = f"effort: {effort}\n"

    agents_dir = Path(output_dir)
    agents_dir.mkdir(parents=True, exist_ok=True)
    dest = agents_dir / f"{name}.md"

    with open(dest, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"Created {dest}")


if __name__ == "__main__":
    main()
