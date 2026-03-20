"""Shared utilities for multi-agent-graph scripts.

Centralizes agent path resolution, frontmatter parsing, and model label
normalization so each script uses a single implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Agent path resolution
# ---------------------------------------------------------------------------

def agent_path_candidates(run_dir: Path, agent_file: str) -> list[Path]:
    """Return plausible paths for an agent markdown reference.

    Execution plans may store agent files either as bare filenames
    (``classicist.md``) or as run-relative paths (``agents/classicist.md``).
    """
    raw = Path(agent_file)
    candidates: list[Path] = []

    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(run_dir / raw)
        candidates.append(PLUGIN_ROOT / raw)
        candidates.append(run_dir / "agents" / raw.name)
        candidates.append(PLUGIN_ROOT / "agents" / raw.name)

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def resolve_agent_path(
    run_dir: Path,
    agent_name: str,
    agent_file: str | None = None,
) -> Path | None:
    """Resolve the markdown file that defines an agent.

    Builds a candidate list from *agent_file* (if given) and *agent_name*,
    then returns the first path that exists on disk, or ``None``.
    """
    candidates: list[Path] = []

    if agent_file:
        candidates.extend(agent_path_candidates(run_dir, agent_file))

    # Also try {agent_name}.md directly
    name_candidates = agent_path_candidates(run_dir, f"{agent_name}.md")
    for c in name_candidates:
        if str(c) not in {str(p) for p in candidates}:
            candidates.append(c)

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def read_agent_frontmatter(path: Path) -> dict[str, str]:
    """Parse simple YAML frontmatter from an agent markdown file.

    Returns a dict mapping lowercase key names to their string values.
    Returns an empty dict if the file cannot be read or has no frontmatter.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    if not lines or lines[0].strip() != "---":
        return {}

    frontmatter: dict[str, str] = {}
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        frontmatter[key.strip().lower()] = value.strip()
    return frontmatter


# ---------------------------------------------------------------------------
# Model label normalization
# ---------------------------------------------------------------------------

def normalize_model_label(raw_model: Optional[str]) -> str:
    """Collapse raw model identifiers to a simple display label."""
    if not raw_model:
        return "Unknown"

    lower = raw_model.strip().lower()
    if "haiku" in lower:
        return "Haiku"
    if "sonnet" in lower:
        return "Sonnet"
    if "opus" in lower:
        return "Opus"
    return raw_model.strip()
