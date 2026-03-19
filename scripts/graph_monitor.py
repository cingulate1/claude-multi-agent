#!/usr/bin/env python3
"""Live graph monitor GUI for multi-agent-graph execution runs.

Polls status.json and renders the directed execution graph on a Tkinter canvas,
updating node colors and status text in real time.

Usage:
    python graph_monitor.py <run_dir>

Reads execution_plan.json (once) and status.json (polled every second) from run_dir.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLL_INTERVAL_MS = 1000
TIMER_INTERVAL_MS = 200

WINDOW_WIDTH = 900
WINDOW_HEIGHT = 700
CANVAS_PAD = 60
CANVAS_USABLE_FRACTION = 0.7814  # Events panel occupies ~21.86% of window height

NODE_WIDTH = 220
NODE_HEIGHT = 74
NODE_CORNER_RADIUS = 10
NODE_MARGIN_X = 20  # Minimum margin between node edge and canvas edge
NODE_FONT = ("Segoe UI", 8)
NODE_FONT_BOLD = ("Segoe UI", 9, "bold")
MODEL_COLOR = "#475569"
TOKEN_FONT = ("Segoe UI", 8)
TOKEN_COLOR = "#64748B"
CYCLE_LABEL_FONT = ("Segoe UI", 8, "italic")

BG_COLOR = "#F8FAFC"
CANVAS_BG = "#F8FAFC"
STATUS_BAR_BG = "#E2E8F0"
EVENTS_BG = "#F1F5F9"
EVENTS_BORDER = "#CBD5E1"

STATE_COLORS = {
    "pending":   {"fill": "#E5E7EB", "border": "#9CA3AF", "width": 1.5},
    "running":   {"fill": "#DBEAFE", "border": "#2563EB", "width": 3},
    "completed": {"fill": "#D1FAE5", "border": "#059669", "width": 1.5},
    "failed":    {"fill": "#FEE2E2", "border": "#DC2626", "width": 2},
}

ARROW_COLOR = "#64748B"
ARROW_WIDTH = 1.5
ARROWHEAD_SIZE = 8


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _node_anchor(cx: float, cy: float, target_x: float, target_y: float) -> Tuple[float, float]:
    """Find the point on the rounded-rect border closest to (target_x, target_y)."""
    hw, hh = NODE_WIDTH / 2, NODE_HEIGHT / 2
    dx = target_x - cx
    dy = target_y - cy
    if abs(dx) < 1 and abs(dy) < 1:
        return cx, cy - hh  # default to top

    # Scale to hit the rectangle edge
    sx = hw / abs(dx) if abs(dx) > 0.01 else 1e6
    sy = hh / abs(dy) if abs(dy) > 0.01 else 1e6
    s = min(sx, sy)
    return cx + dx * s, cy + dy * s


def _bezier_point(t: float, pts: List[Tuple[float, float]]) -> Tuple[float, float]:
    """Evaluate a cubic bezier at parameter t."""
    p0, p1, p2, p3 = pts
    u = 1 - t
    x = u**3*p0[0] + 3*u**2*t*p1[0] + 3*u*t**2*p2[0] + t**3*p3[0]
    y = u**3*p0[1] + 3*u**2*t*p1[1] + 3*u*t**2*p2[1] + t**3*p3[1]
    return x, y


def _extract_frontmatter_model(agent_path: Path) -> Optional[str]:
    """Return the raw `model:` value from an agent markdown frontmatter block."""
    try:
        lines = agent_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    if not lines or lines[0].strip() != "---":
        return None

    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        if key.strip().lower() == "model":
            return value.strip()
    return None


def _normalize_model_label(raw_model: Optional[str]) -> str:
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


def _agent_path_candidates(run_dir: Path, agent_file: str) -> list[Path]:
    """Return plausible run-relative/plugin-relative agent markdown paths."""
    plugin_root = Path(__file__).resolve().parent.parent
    raw = Path(agent_file)
    candidates: list[Path] = []

    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(run_dir / raw)
        candidates.append(plugin_root / raw)
        candidates.append(run_dir / "agents" / raw.name)
        candidates.append(plugin_root / "agents" / raw.name)

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def load_node_models(run_dir: Path, plan: Dict[str, Any]) -> Dict[str, str]:
    """Resolve display model labels for each node from its agent markdown."""
    node_models: Dict[str, str] = {}

    for node in plan.get("nodes", []):
        name = node["name"]
        agent_file = node.get("agent_file")
        raw_model = None
        if agent_file:
            for candidate in _agent_path_candidates(run_dir, agent_file):
                raw_model = _extract_frontmatter_model(candidate)
                if raw_model:
                    break
        node_models[name] = _normalize_model_label(raw_model)

    return node_models


# ---------------------------------------------------------------------------
# Layout computation
# ---------------------------------------------------------------------------

def compute_layout(
    plan: Dict[str, Any],
    canvas_width: int,
    canvas_height: int,
) -> Dict[str, Tuple[float, float]]:
    """Compute node center positions based on the pattern type.

    Returns {node_name: (cx, cy)}.
    """
    # Effective height excludes the events panel area
    effective_height = int(canvas_height * CANVAS_USABLE_FRACTION)

    nodes = plan["nodes"]
    pattern = plan.get("pattern", "")
    cycles = plan.get("cycles", [])
    names = [n["name"] for n in nodes]

    if not names:
        return {}

    usable_w = canvas_width - NODE_WIDTH - 2 * NODE_MARGIN_X
    usable_h = effective_height - 2 * CANVAS_PAD
    left = CANVAS_PAD
    top = CANVAS_PAD

    # Helpers
    def center_one() -> Dict[str, Tuple[float, float]]:
        cx = canvas_width / 2
        cy = effective_height / 2
        return {names[0]: (cx, cy)}

    def row_positions(row_names: List[str], y: float) -> Dict[str, Tuple[float, float]]:
        if not row_names:
            return {}
        n = len(row_names)
        if n == 1:
            return {row_names[0]: (canvas_width / 2, y)}
        spacing = min(usable_w / (n - 1), NODE_WIDTH * 1.6)
        total = spacing * (n - 1)
        start_x = (canvas_width - total) / 2
        return {name: (start_x + i * spacing, y) for i, name in enumerate(row_names)}

    # --- Chained iteration (self-loop) ---
    if pattern == "chained-iteration":
        return center_one()

    # --- Bipartite cycles (rag-grounded, rubric-based and their variants) ---
    if pattern.startswith("rag-grounded") or pattern.startswith("rubric-based"):
        if len(names) <= 3:
            cx = canvas_width / 2
            gap = NODE_HEIGHT * 1.6
            total_h = gap * (len(names) - 1)
            start_y = effective_height / 2 - total_h / 2
            return {name: (cx, start_y + i * gap) for i, name in enumerate(names)}
        # Fallback for unexpected node counts
        return _layered_layout(nodes, canvas_width, effective_height)

    # --- Consensus panel / Debate panel ---
    if pattern in ("consensus-panel", "debate-panel"):
        return _panel_layout(nodes, canvas_width, effective_height)

    # --- Dissensus integration ---
    if pattern == "dissensus-integration":
        # Row 1: panelists (no deps or deps only on each other in same group)
        # Row 2: integrator
        panelists = [n["name"] for n in nodes if not n.get("depends_on")]
        rest = [n["name"] for n in nodes if n.get("depends_on")]
        if not rest:
            rest = [panelists.pop()] if len(panelists) > 1 else []

        positions = {}
        y1 = top + NODE_HEIGHT
        y2 = top + usable_h - NODE_HEIGHT / 2
        positions.update(row_positions(panelists, y1))
        positions.update(row_positions(rest, y2))
        return positions

    # --- Parallel decomposition ---
    if pattern == "parallel-decomposition":
        return _decomposition_layout(nodes, canvas_width, effective_height)

    # --- Unknown pattern: fall back to layered layout ---
    return _layered_layout(nodes, canvas_width, effective_height)


def _panel_layout(
    nodes: List[Dict],
    canvas_width: int,
    canvas_height: int,
) -> Dict[str, Tuple[float, float]]:
    """Layered layout for panel patterns (consensus, debate).

    Groups nodes by parallel_group, then orders groups by dependency depth.
    """
    usable_w = canvas_width - NODE_WIDTH - 2 * NODE_MARGIN_X
    usable_h = canvas_height - 2 * CANVAS_PAD

    # Group by parallel_group (or None)
    groups: Dict[Optional[str], List[str]] = {}
    for n in nodes:
        g = n.get("parallel_group")
        groups.setdefault(g, []).append(n["name"])

    # Order groups by minimum dependency depth
    name_to_node = {n["name"]: n for n in nodes}
    depth_cache: Dict[str, int] = {}

    def depth(name: str) -> int:
        if name in depth_cache:
            return depth_cache[name]
        deps = name_to_node.get(name, {}).get("depends_on", [])
        if not deps:
            depth_cache[name] = 0
        else:
            depth_cache[name] = 1 + max(depth(d) for d in deps)
        return depth_cache[name]

    for n in nodes:
        depth(n["name"])

    def group_min_depth(members: List[str]) -> int:
        return min(depth_cache.get(m, 0) for m in members)

    sorted_groups = sorted(groups.values(), key=group_min_depth)

    num_rows = len(sorted_groups)
    if num_rows == 0:
        return {}

    row_spacing = usable_h / max(num_rows - 1, 1) if num_rows > 1 else 0
    start_y = CANVAS_PAD + NODE_HEIGHT

    if num_rows == 1:
        start_y = canvas_height / 2

    positions = {}
    for row_idx, members in enumerate(sorted_groups):
        y = start_y + row_idx * row_spacing if num_rows > 1 else start_y
        n = len(members)
        if n == 1:
            positions[members[0]] = (canvas_width / 2, y)
        else:
            spacing = min(usable_w / max(n - 1, 1), NODE_WIDTH * 1.6)
            total = spacing * (n - 1)
            sx = (canvas_width - total) / 2
            for i, name in enumerate(members):
                positions[name] = (sx + i * spacing, y)

    return positions


def _decomposition_layout(
    nodes: List[Dict],
    canvas_width: int,
    canvas_height: int,
) -> Dict[str, Tuple[float, float]]:
    """Layout for one decomposer feeding many independent workers."""
    usable_w = canvas_width - NODE_WIDTH - 2 * NODE_MARGIN_X
    usable_h = canvas_height - 2 * CANVAS_PAD

    decomposer = [n["name"] for n in nodes if not n.get("depends_on")]
    workers = [n["name"] for n in nodes if n.get("depends_on")]
    if not decomposer:
        decomposer = [nodes[0]["name"]] if nodes else []
        workers = [n["name"] for n in nodes[1:]]

    positions: Dict[str, Tuple[float, float]] = {}
    if decomposer:
        positions.update({decomposer[0]: (canvas_width / 2, CANVAS_PAD + NODE_HEIGHT)})
        for extra_index, name in enumerate(decomposer[1:], start=1):
            positions[name] = (
                canvas_width / 2 + (extra_index * (NODE_WIDTH * 1.15)),
                CANVAS_PAD + NODE_HEIGHT,
            )

    if not workers:
        return positions

    max_cols = max(1, int((canvas_width - 2 * NODE_MARGIN_X) // (NODE_WIDTH + 24)))
    num_rows = math.ceil(len(workers) / max_cols)

    top_y = CANVAS_PAD + NODE_HEIGHT + 110
    bottom_y = canvas_height - CANVAS_PAD - NODE_HEIGHT / 2
    if num_rows == 1:
        row_ys = [(top_y + bottom_y) / 2]
    else:
        row_step = (bottom_y - top_y) / max(num_rows - 1, 1)
        row_ys = [top_y + (row_step * i) for i in range(num_rows)]

    for row_index in range(num_rows):
        row_names = workers[row_index * max_cols:(row_index + 1) * max_cols]
        if not row_names:
            continue
        y = row_ys[min(row_index, len(row_ys) - 1)]
        if len(row_names) == 1:
            positions[row_names[0]] = (canvas_width / 2, y)
            continue

        spacing = min(usable_w / max(len(row_names) - 1, 1), NODE_WIDTH * 1.2)
        total = spacing * (len(row_names) - 1)
        start_x = (canvas_width - total) / 2
        for i, name in enumerate(row_names):
            positions[name] = (start_x + i * spacing, y)

    return positions


def _layered_layout(
    nodes: List[Dict],
    canvas_width: int,
    canvas_height: int,
) -> Dict[str, Tuple[float, float]]:
    """Generic layered layout by topological depth."""
    usable_w = canvas_width - NODE_WIDTH - 2 * NODE_MARGIN_X
    usable_h = canvas_height - 2 * CANVAS_PAD

    name_to_node = {n["name"]: n for n in nodes}
    depth_cache: Dict[str, int] = {}

    def depth(name: str) -> int:
        if name in depth_cache:
            return depth_cache[name]
        deps = name_to_node.get(name, {}).get("depends_on", [])
        if not deps:
            depth_cache[name] = 0
        else:
            depth_cache[name] = 1 + max(depth(d) for d in deps if d in name_to_node)
        return depth_cache[name]

    for n in nodes:
        depth(n["name"])

    # Group by depth
    layers: Dict[int, List[str]] = {}
    for n in nodes:
        d = depth_cache[n["name"]]
        layers.setdefault(d, []).append(n["name"])

    sorted_layers = sorted(layers.items())
    num_rows = len(sorted_layers)
    if num_rows == 0:
        return {}

    row_spacing = usable_h / max(num_rows - 1, 1) if num_rows > 1 else 0
    start_y = CANVAS_PAD + NODE_HEIGHT
    if num_rows == 1:
        start_y = canvas_height / 2

    positions = {}
    for row_idx, (_, members) in enumerate(sorted_layers):
        y = start_y + row_idx * row_spacing if num_rows > 1 else start_y
        n = len(members)
        if n == 1:
            positions[members[0]] = (canvas_width / 2, y)
        else:
            spacing = min(usable_w / max(n - 1, 1), NODE_WIDTH * 1.6)
            total = spacing * (n - 1)
            sx = (canvas_width - total) / 2
            for i, name in enumerate(members):
                positions[name] = (sx + i * spacing, y)

    return positions


# ---------------------------------------------------------------------------
# Edge topology extraction
# ---------------------------------------------------------------------------

class EdgeInfo:
    """Describes one visual edge to draw."""
    __slots__ = ("src", "dst", "edge_type")

    def __init__(self, src: str, dst: str, edge_type: str = "normal"):
        self.src = src
        self.dst = dst
        self.edge_type = edge_type  # "normal", "self_loop", "bipartite_fwd", "bipartite_rev"


def extract_edges(plan: Dict[str, Any]) -> List[EdgeInfo]:
    """Build the list of visual edges from the execution plan."""
    edges: List[EdgeInfo] = []
    nodes = plan["nodes"]
    cycles = plan.get("cycles", [])

    # Collect cycle participant pairs to avoid duplicating them as normal edges
    cycle_pairs: set = set()
    for cycle in cycles:
        if cycle["type"] == "self-loop":
            agent = cycle["agent"]
            edges.append(EdgeInfo(agent, agent, "self_loop"))
            cycle_pairs.add((agent, agent))
        elif cycle["type"] == "bipartite":
            p, e = cycle["producer"], cycle["evaluator"]
            edges.append(EdgeInfo(p, e, "bipartite_fwd"))
            edges.append(EdgeInfo(e, p, "bipartite_rev"))
            cycle_pairs.add((p, e))
            cycle_pairs.add((e, p))

    # Normal dependency edges
    for node in nodes:
        dst = node["name"]
        for dep in node.get("depends_on", []):
            if (dep, dst) not in cycle_pairs:
                edges.append(EdgeInfo(dep, dst, "normal"))

    return edges


# ---------------------------------------------------------------------------
# Canvas drawing
# ---------------------------------------------------------------------------

class GraphRenderer:
    """Manages all canvas drawing for the execution graph."""

    def __init__(self, canvas: tk.Canvas):
        self.canvas = canvas
        self.positions: Dict[str, Tuple[float, float]] = {}
        self.node_ids: Dict[str, Dict[str, int]] = {}  # name -> {rect, text, model_text, token_text}
        self.node_models: Dict[str, str] = {}
        self.node_types: Dict[str, str] = {}
        self.edge_ids: List[int] = []
        self.cycle_label_ids: Dict[str, int] = {}
        self.edges: List[EdgeInfo] = []
        self.plan: Optional[Dict[str, Any]] = None
        # Drag state
        self._user_positions: Dict[str, Tuple[float, float]] = {}
        self._drag_node: Optional[str] = None
        self._drag_offset: Tuple[float, float] = (0.0, 0.0)
        self._item_to_node: Dict[int, str] = {}  # canvas item id -> node name

    def set_plan(
        self,
        plan: Dict[str, Any],
        width: int,
        height: int,
        node_models: Optional[Dict[str, str]] = None,
    ) -> None:
        self.plan = plan
        self.node_models = node_models or {}
        self.node_types = {
            node["name"]: node.get("node_type", "agent")
            for node in plan.get("nodes", [])
        }
        self.positions = compute_layout(plan, width, height)
        self.positions.update(self._user_positions)
        self.edges = extract_edges(plan)
        self._draw_all()

    def resize(self, width: int, height: int) -> None:
        if self.plan is None:
            return
        self.positions = compute_layout(self.plan, width, height)
        self.positions.update(self._user_positions)
        self._draw_all()

    def _draw_all(self) -> None:
        self.canvas.delete("all")
        self.node_ids.clear()
        self.edge_ids.clear()
        self.cycle_label_ids.clear()
        self._item_to_node.clear()

        # Draw edges first (behind nodes)
        for edge in self.edges:
            self._draw_edge(edge)

        # Draw nodes on top
        for name, (cx, cy) in self.positions.items():
            self._draw_node(name, cx, cy)

        # Register item-to-node mapping and bind drag events
        for name, ids in self.node_ids.items():
            for item_id in ids.values():
                self._item_to_node[item_id] = name
                self.canvas.tag_bind(item_id, "<Button-1>", self._on_node_press)
                self.canvas.tag_bind(item_id, "<B1-Motion>", self._on_node_drag)
                self.canvas.tag_bind(item_id, "<ButtonRelease-1>", self._on_node_release)

    # -- Nodes --

    def _draw_node(self, name: str, cx: float, cy: float) -> None:
        hw, hh = NODE_WIDTH / 2, NODE_HEIGHT / 2
        style = STATE_COLORS["pending"]

        rect_id = self._rounded_rect(
            cx - hw, cy - hh, cx + hw, cy + hh,
            NODE_CORNER_RADIUS,
            fill=style["fill"],
            outline=style["border"],
            width=style["width"],
        )

        display_name = self._truncate(name, 22)
        text_id = self.canvas.create_text(
            cx, cy - 16,
            text=display_name,
            font=NODE_FONT_BOLD,
            fill="#1E293B",
            width=NODE_WIDTH - 16,
            anchor="center",
        )

        is_script = self.node_types.get(name) == "script"

        if is_script:
            subtitle_text = "Script"
            token_text = ""
        else:
            model_label = _normalize_model_label(self.node_models.get(name))
            subtitle_text = f"Model: {model_label}"
            token_text = "Tokens: 0"

        model_id = self.canvas.create_text(
            cx, cy,
            text=subtitle_text,
            font=NODE_FONT,
            fill=MODEL_COLOR,
            width=NODE_WIDTH - 16,
            anchor="center",
        )

        token_id = self.canvas.create_text(
            cx, cy + 16,
            text=token_text,
            font=TOKEN_FONT,
            fill=TOKEN_COLOR,
            width=NODE_WIDTH - 16,
            anchor="center",
        )

        self.node_ids[name] = {
            "rect": rect_id,
            "text": text_id,
            "model_text": model_id,
            "token_text": token_id,
        }

    def _rounded_rect(
        self, x1: float, y1: float, x2: float, y2: float,
        r: float, **kwargs
    ) -> int:
        points = [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1,
        ]
        return self.canvas.create_polygon(points, smooth=True, **kwargs)

    @staticmethod
    def _truncate(name: str, max_chars: int = 28) -> str:
        if len(name) <= max_chars:
            return name
        return name[:max_chars - 1] + "\u2026"

    # -- Drag handling --

    def _on_node_press(self, event: tk.Event) -> None:
        item = self.canvas.find_closest(event.x, event.y)
        if not item:
            return
        name = self._item_to_node.get(item[0])
        if name is None:
            return
        cx, cy = self.positions[name]
        self._drag_node = name
        self._drag_offset = (event.x - cx, event.y - cy)

    def _on_node_drag(self, event: tk.Event) -> None:
        name = self._drag_node
        if name is None:
            return
        ox, oy = self._drag_offset
        new_cx = event.x - ox
        new_cy = event.y - oy
        old_cx, old_cy = self.positions[name]
        dx = new_cx - old_cx
        dy = new_cy - old_cy

        # Move all canvas items belonging to this node
        ids = self.node_ids.get(name, {})
        for item_id in ids.values():
            self.canvas.move(item_id, dx, dy)

        # Update stored position
        self.positions[name] = (new_cx, new_cy)
        self._user_positions[name] = (new_cx, new_cy)

        # Redraw all edges to reflect the new position
        self._redraw_edges()

    def _on_node_release(self, event: tk.Event) -> None:
        self._drag_node = None

    def _redraw_edges(self) -> None:
        """Delete and redraw all edges and cycle labels."""
        for eid in self.edge_ids:
            self.canvas.delete(eid)
        self.edge_ids.clear()
        for lid in self.cycle_label_ids.values():
            self.canvas.delete(lid)
        self.cycle_label_ids.clear()

        for edge in self.edges:
            self._draw_edge(edge)

    # -- Edges --

    def _draw_edge(self, edge: EdgeInfo) -> None:
        if edge.src not in self.positions or edge.dst not in self.positions:
            return

        if edge.edge_type == "self_loop":
            self._draw_self_loop(edge.src)
        elif edge.edge_type in ("bipartite_fwd", "bipartite_rev"):
            self._draw_bipartite_arc(edge)
        else:
            self._draw_normal_arrow(edge.src, edge.dst)

    def _draw_normal_arrow(self, src: str, dst: str) -> None:
        sx, sy = self.positions[src]
        dx, dy = self.positions[dst]

        # Anchor points on node borders
        ax, ay = _node_anchor(sx, sy, dx, dy)
        bx, by = _node_anchor(dx, dy, sx, sy)

        line_id = self.canvas.create_line(
            ax, ay, bx, by,
            fill=ARROW_COLOR, width=ARROW_WIDTH, smooth=False,
        )
        self.edge_ids.append(line_id)

        # Arrowhead
        self._draw_arrowhead(ax, ay, bx, by)

    def _draw_arrowhead(self, x1: float, y1: float, x2: float, y2: float) -> None:
        angle = math.atan2(y2 - y1, x2 - x1)
        s = ARROWHEAD_SIZE
        ax = x2 - s * math.cos(angle - math.pi / 7)
        ay = y2 - s * math.sin(angle - math.pi / 7)
        bx = x2 - s * math.cos(angle + math.pi / 7)
        by = y2 - s * math.sin(angle + math.pi / 7)
        self.canvas.create_polygon(
            x2, y2, ax, ay, bx, by,
            fill=ARROW_COLOR, outline=ARROW_COLOR,
        )

    def _draw_self_loop(self, name: str) -> None:
        cx, cy = self.positions[name]
        hw = NODE_WIDTH / 2
        hh = NODE_HEIGHT / 2

        # Arc from top-right back to top-left
        arc_h = 45
        x_start = cx + hw * 0.4
        y_start = cy - hh
        x_end = cx - hw * 0.4
        y_end = cy - hh

        cp1 = (x_start + 30, y_start - arc_h)
        cp2 = (x_end - 30, y_end - arc_h)

        # Draw as line segments approximating cubic bezier
        pts = [(x_start, y_start), cp1, cp2, (x_end, y_end)]
        coords = []
        steps = 20
        for i in range(steps + 1):
            t = i / steps
            px, py = _bezier_point(t, pts)
            coords.extend([px, py])

        line_id = self.canvas.create_line(
            *coords,
            fill=ARROW_COLOR, width=ARROW_WIDTH, smooth=True,
        )
        self.edge_ids.append(line_id)

        # Arrowhead at end
        t_near = (steps - 1) / steps
        near_x, near_y = _bezier_point(t_near, pts)
        self._draw_arrowhead(near_x, near_y, x_end, y_end)

        # Cycle label above the loop
        label_x = cx
        label_y = y_start - arc_h - 8
        label_id = self.canvas.create_text(
            label_x, label_y,
            text="",
            font=CYCLE_LABEL_FONT,
            fill="#6366F1",
            anchor="center",
        )
        self.cycle_label_ids[name] = label_id

    def _draw_bipartite_arc(self, edge: EdgeInfo) -> None:
        sx, sy = self.positions[edge.src]
        dx, dy = self.positions[edge.dst]

        is_fwd = edge.edge_type == "bipartite_fwd"
        arc_offset = 35 if is_fwd else 55

        # Determine arc direction (bend above or below the line)
        mid_x = (sx + dx) / 2
        mid_y = (sy + dy) / 2

        # Perpendicular offset for the control point
        line_dx = dx - sx
        line_dy = dy - sy
        length = math.sqrt(line_dx**2 + line_dy**2) or 1
        perp_x = -line_dy / length
        perp_y = line_dx / length

        sign = -1 if is_fwd else 1
        cp_x = mid_x + sign * perp_x * arc_offset
        cp_y = mid_y + sign * perp_y * arc_offset

        # Anchor on node borders toward control point
        ax, ay = _node_anchor(sx, sy, cp_x, cp_y)
        bx, by = _node_anchor(dx, dy, cp_x, cp_y)

        # Quadratic bezier approximated with cubic (duplicate control point)
        pts = [(ax, ay), (cp_x, cp_y), (cp_x, cp_y), (bx, by)]
        coords = []
        steps = 20
        for i in range(steps + 1):
            t = i / steps
            px, py = _bezier_point(t, pts)
            coords.extend([px, py])

        line_id = self.canvas.create_line(
            *coords,
            fill=ARROW_COLOR, width=ARROW_WIDTH, smooth=True,
        )
        self.edge_ids.append(line_id)

        # Arrowhead at destination
        t_near = (steps - 1) / steps
        near_x, near_y = _bezier_point(t_near, pts)
        self._draw_arrowhead(near_x, near_y, bx, by)

        # Cycle round label between the two arcs (only once, for fwd)
        if is_fwd:
            key = f"{edge.src}-{edge.dst}"
            label_id = self.canvas.create_text(
                mid_x, mid_y,
                text="",
                font=CYCLE_LABEL_FONT,
                fill="#6366F1",
                anchor="center",
            )
            self.cycle_label_ids[key] = label_id

    # -- State updates --

    def update_states(
        self,
        node_states: Dict[str, Dict[str, Any]],
        cycle_states: Dict[str, Dict[str, Any]],
    ) -> None:
        """Update node colors, model labels, token labels, and cycle labels."""
        for name, ids in self.node_ids.items():
            node_data = node_states.get(name, {})
            state = node_data.get("state", "pending")
            style = STATE_COLORS.get(state, STATE_COLORS["pending"])

            self.canvas.itemconfigure(ids["rect"],
                fill=style["fill"],
                outline=style["border"],
                width=style["width"],
            )

            if self.node_types.get(name) != "script":
                model_label = _normalize_model_label(
                    node_data.get("model") or self.node_models.get(name)
                )
                self.canvas.itemconfigure(
                    ids["model_text"],
                    text=f"Model: {model_label}",
                )

                tokens = node_data.get("tokens", {})
                total_tokens = int(tokens.get("input", 0) or 0) + int(tokens.get("output", 0) or 0)
                self.canvas.itemconfigure(
                    ids["token_text"],
                    text=f"Tokens: {total_tokens:,}",
                )

        # Cycle labels
        for key, label_id in self.cycle_label_ids.items():
            cycle_data = cycle_states.get(key, {})
            current = cycle_data.get("current_round", 0)
            maximum = cycle_data.get("max_rounds", 0)
            c_state = cycle_data.get("state", "pending")
            if c_state == "running" and current > 0:
                self.canvas.itemconfigure(label_id,
                    text=f"Round {current}/{maximum}")
            elif c_state == "completed":
                self.canvas.itemconfigure(label_id, text="done")
            elif c_state == "pending":
                self.canvas.itemconfigure(label_id, text="")
            else:
                self.canvas.itemconfigure(label_id, text=c_state)


# ---------------------------------------------------------------------------
# Events panel
# ---------------------------------------------------------------------------

class EventsPanel(tk.Frame):
    """Fixed panel showing the last N events."""

    MAX_EVENTS = 8

    def __init__(self, parent: tk.Widget, **kwargs):
        super().__init__(parent, bg=EVENTS_BG, **kwargs)

        header = tk.Frame(self, bg=EVENTS_BG)
        header.pack(fill="x", padx=6, pady=(4, 0))

        tk.Label(
            header, text="Events", font=("Segoe UI", 9, "bold"),
            bg=EVENTS_BG, fg="#334155", anchor="w",
        ).pack(side="left")

        self._text = tk.Text(
            self, height=self.MAX_EVENTS, wrap="word",
            bg=EVENTS_BG, fg="#475569",
            font=("Consolas", 8),
            relief="flat", borderwidth=0,
            state="disabled",
            padx=6, pady=2,
        )
        self._text.pack(fill="both", expand=True, padx=2, pady=(0, 4))

    def update_events(self, events: List[Dict[str, str]]) -> None:
        recent = events[-self.MAX_EVENTS:] if events else []
        lines = []
        for ev in recent:
            ts = ev.get("ts", "")
            # Extract time portion (HH:MM:SS)
            time_part = ""
            if "T" in ts:
                time_part = ts.split("T")[1][:8]
            level = ev.get("level", "INFO")
            msg = ev.get("message", "")
            prefix = "!" if level == "ERROR" else " "
            lines.append(f"{prefix}{time_part}  {msg}")

        text = "\n".join(lines)
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", text)
        self._text.configure(state="disabled")


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

class StatusBar(tk.Frame):
    """Bottom bar showing pattern, activity, elapsed time, overall state."""

    def __init__(self, parent: tk.Widget, **kwargs):
        super().__init__(parent, bg=STATUS_BAR_BG, **kwargs)

        self._pattern_label = tk.Label(
            self, text="", font=("Segoe UI", 9, "bold"),
            bg=STATUS_BAR_BG, fg="#1E293B", anchor="w",
        )
        self._pattern_label.pack(side="left", padx=(10, 20))

        self._activity_label = tk.Label(
            self, text="", font=("Segoe UI", 9),
            bg=STATUS_BAR_BG, fg="#475569", anchor="w",
        )
        self._activity_label.pack(side="left", fill="x", expand=True)

        self._elapsed_label = tk.Label(
            self, text="", font=("Consolas", 9),
            bg=STATUS_BAR_BG, fg="#64748B", anchor="e",
        )
        self._elapsed_label.pack(side="right", padx=(10, 6))

        self._state_label = tk.Label(
            self, text="", font=("Segoe UI", 9, "bold"),
            bg=STATUS_BAR_BG, fg="#1E293B", anchor="e",
        )
        self._state_label.pack(side="right", padx=(10, 4))

        self._start_time: Optional[datetime] = None
        self._finished = False

    def set_pattern(self, pattern: str) -> None:
        self._pattern_label.configure(text=pattern or "")

    def set_start_time(self, iso_str: Optional[str]) -> None:
        if iso_str:
            try:
                self._start_time = datetime.fromisoformat(iso_str)
            except (ValueError, TypeError):
                self._start_time = None

    def update_status(self, status: Dict[str, Any]) -> None:
        activity = status.get("activity", "")
        state = status.get("state", "idle")

        self._activity_label.configure(text=activity)

        state_colors = {
            "idle": "#64748B",
            "running": "#2563EB",
            "completed": "#059669",
            "failed": "#DC2626",
        }
        color = state_colors.get(state, "#64748B")
        self._state_label.configure(text=state.upper(), fg=color)
        self._finished = state in ("completed", "failed")

    def tick_elapsed(self) -> None:
        if self._start_time is None:
            self._elapsed_label.configure(text="")
            return

        if self._finished:
            return  # freeze elapsed time

        now = datetime.now(timezone.utc).astimezone()
        delta = now - self._start_time
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            total_seconds = 0
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        if hours > 0:
            text = f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            text = f"{minutes}:{seconds:02d}"
        self._elapsed_label.configure(text=text)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class GraphMonitorApp:
    """Top-level application managing the graph monitor window."""

    def __init__(self, run_dir: Path, geometry: Optional[str] = None):
        self.run_dir = run_dir.resolve()
        self.plan_path = self.run_dir / "execution_plan.json"
        self.status_path = self.run_dir / "logs" / "status.json"

        self.plan: Optional[Dict[str, Any]] = None
        self._plan_mtime_ns: Optional[int] = None
        self.last_status: Optional[Dict[str, Any]] = None
        self._polling_active = True

        # -- Window setup --
        self.root = tk.Tk()
        self.root.title("multi-agent-graph")
        geo = geometry or f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}"
        self.root.geometry(geo)
        self.root.minsize(600, 400)
        self.root.configure(bg=BG_COLOR)

        # Try to set icon (non-fatal if missing)
        try:
            icon_path = Path(__file__).parent.parent / "assets" / "icon.ico"
            if icon_path.exists():
                self.root.iconbitmap(str(icon_path))
        except tk.TclError:
            pass

        # -- Layout --
        self._build_ui()

        # -- Load plan --
        self._try_load_plan()

        # -- Start polling --
        self._schedule_poll()
        self._schedule_timer()

    def _build_ui(self) -> None:
        # Main canvas
        self.canvas = tk.Canvas(
            self.root, bg=CANVAS_BG,
            highlightthickness=0,
        )
        self.canvas.pack(fill="both", expand=True)

        # Separator line above events panel
        sep = tk.Frame(self.root, height=1, bg=EVENTS_BORDER)
        sep.pack(fill="x")

        # Events panel
        self.events_panel = EventsPanel(self.root, height=110)
        self.events_panel.pack(fill="x")

        # Separator line above status bar
        sep2 = tk.Frame(self.root, height=1, bg="#CBD5E1")
        sep2.pack(fill="x")

        # Status bar
        self.status_bar = StatusBar(self.root, height=32)
        self.status_bar.pack(fill="x")

        # Renderer
        self.renderer = GraphRenderer(self.canvas)

        # Waiting label (shown before plan loads)
        self._waiting_label = self.canvas.create_text(
            0, 0,
            text="Waiting for execution to start\u2026",
            font=("Segoe UI", 12),
            fill="#94A3B8",
            anchor="center",
        )
        self._update_waiting_pos()

        # Bind resize
        self.canvas.bind("<Configure>", self._on_canvas_resize)

    def _update_waiting_pos(self) -> None:
        w = self.canvas.winfo_width() or WINDOW_WIDTH
        h = self.canvas.winfo_height() or (WINDOW_HEIGHT - 170)
        self.canvas.coords(self._waiting_label, w / 2, h / 2)

    def _on_canvas_resize(self, event: tk.Event) -> None:
        if self.plan is not None:
            self.renderer.resize(event.width, event.height)
            # Re-apply last known states after redraw
            if self.last_status:
                self.renderer.update_states(
                    self.last_status.get("nodes", {}),
                    self.last_status.get("cycles", {}),
                )
        else:
            self._update_waiting_pos()

    def _try_load_plan(self, force: bool = False) -> bool:
        if not self.plan_path.exists():
            return False
        try:
            plan_stat = self.plan_path.stat()
            mtime_ns = plan_stat.st_mtime_ns
            if not force and self.plan is not None and self._plan_mtime_ns == mtime_ns:
                return True
            text = self.plan_path.read_text(encoding="utf-8")
            loaded_plan = json.loads(text)
        except (OSError, json.JSONDecodeError):
            return False

        self.plan = loaded_plan
        self._plan_mtime_ns = mtime_ns

        pattern = self.plan.get("pattern", "unknown")
        self.root.title(f"multi-agent-graph: {pattern}")
        self.status_bar.set_pattern(pattern)

        # Hide waiting label and draw graph
        self.canvas.delete(self._waiting_label)
        w = self.canvas.winfo_width() or WINDOW_WIDTH
        h = self.canvas.winfo_height() or (WINDOW_HEIGHT - 170)
        node_models = load_node_models(self.run_dir, self.plan)
        self.renderer.set_plan(self.plan, w, h, node_models=node_models)
        if self.last_status:
            self.renderer.update_states(
                self.last_status.get("nodes", {}),
                self.last_status.get("cycles", {}),
            )
        return True

    def _read_status(self) -> Optional[Dict[str, Any]]:
        if not self.status_path.exists():
            return None
        try:
            text = self.status_path.read_text(encoding="utf-8")
            return json.loads(text)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    # -- Polling --

    def _schedule_poll(self) -> None:
        if not self._polling_active:
            return
        self.root.after(POLL_INTERVAL_MS, self._poll)

    def _poll(self) -> None:
        if not self._polling_active:
            return

        # Load or reload the execution plan if it changes on disk.
        if not self._try_load_plan():
            self._schedule_poll()
            return

        status = self._read_status()
        if status is not None:
            self.last_status = status
            self._apply_status(status)

            state = status.get("state", "")
            if state in ("completed", "failed"):
                self._polling_active = False
                return

        self._schedule_poll()

    def _apply_status(self, status: Dict[str, Any]) -> None:
        # Update node and cycle visuals
        self.renderer.update_states(
            status.get("nodes", {}),
            status.get("cycles", {}),
        )

        # Update status bar
        self.status_bar.update_status(status)
        created = status.get("created_at")
        if created:
            self.status_bar.set_start_time(created)

        # Update events panel
        self.events_panel.update_events(status.get("events", []))

    # -- Timer (elapsed clock) --

    def _schedule_timer(self) -> None:
        self.status_bar.tick_elapsed()
        self.root.after(TIMER_INTERVAL_MS, self._schedule_timer)

    # -- Run --

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live graph monitor for multi-agent-graph execution runs",
    )
    parser.add_argument(
        "run_dir",
        help="Path to the run directory containing execution_plan.json and logs/status.json",
    )
    parser.add_argument(
        "--geometry",
        default=None,
        help="Window geometry in Tk format: WxH+X+Y (e.g., 900x700+100+100)",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"Error: '{run_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)

    app = GraphMonitorApp(run_dir, geometry=args.geometry)
    app.run()


if __name__ == "__main__":
    main()
