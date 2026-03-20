#!/usr/bin/env python3
"""Web-based graph monitor for multi-agent-graph execution runs.

This keeps the existing ``python graph_monitor.py <run_dir>`` contract used by
the orchestrator, but replaces the fragile Tkinter canvas with a local HTTP
server that serves a React UI plus JSON snapshot endpoints.
"""

from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from shared import PLUGIN_ROOT, agent_path_candidates, normalize_model_label, read_agent_frontmatter


POLL_INTERVAL_S = 0.5
STALE_THRESHOLD_S = 30
TIMELINE_TAIL_LINES = 180
PREVIEW_CHAR_LIMIT = 12000

WEB_APP_DIST = PLUGIN_ROOT / "web" / "graph-monitor" / "dist"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _path_stats(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"exists": False, "mtimeNs": None, "sizeBytes": None}
    try:
        stat = path.stat()
    except OSError:
        return {"exists": True, "mtimeNs": None, "sizeBytes": None}
    return {"exists": True, "mtimeNs": stat.st_mtime_ns, "sizeBytes": stat.st_size}


def _tail_lines(path: Path, line_count: int) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end_pos = handle.tell()
            block_size = 8192
            data = b""
            cursor = end_pos

            while cursor > 0 and data.count(b"\n") <= line_count:
                read_size = min(block_size, cursor)
                cursor -= read_size
                handle.seek(cursor)
                data = handle.read(read_size) + data

            return data.decode("utf-8", errors="replace").splitlines()[-line_count:]
    except OSError:
        return []


def _read_timeline(path: Path, line_count: int = TIMELINE_TAIL_LINES) -> list[Dict[str, Any]]:
    events: list[Dict[str, Any]] = []
    for line in _tail_lines(path, line_count):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return events


def _parse_run_status(text: str) -> Optional[Dict[str, Any]]:
    if not text.strip():
        return None

    updated_at = None
    elapsed = None
    rows = []
    header_index = -1
    lines = text.splitlines()

    for line in lines:
        if line.startswith("Updated:"):
            parts = [part.strip() for part in line.split("|")]
            if parts:
                updated_at = parts[0].replace("Updated:", "", 1).strip()
            if len(parts) > 1:
                elapsed = parts[1].replace("Elapsed:", "", 1).strip()
            break

    for index, line in enumerate(lines):
        if line.strip().startswith("| Agent | State |"):
            header_index = index
            break

    if header_index >= 0:
        for line in lines[header_index + 2:]:
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            columns = [column.strip() for column in stripped.strip("|").split("|")]
            if len(columns) < 9:
                continue
            rows.append(
                {
                    "agent": columns[0],
                    "state": columns[1].lower(),
                    "elapsed": None if columns[2] in {"-", "—"} else columns[2],
                    "compacted": columns[3].lower() == "yes",
                    "tokensIn": None if columns[4] in {"-", "—"} else columns[4],
                    "tokensOut": None if columns[5] in {"-", "—"} else columns[5],
                    "filesRead": None if columns[6] in {"-", "—"} else columns[6],
                    "outputKb": None if columns[7] in {"-", "—"} else columns[7],
                    "complete": columns[8].lower() == "yes",
                }
            )

    return {
        "updatedAt": updated_at,
        "elapsed": elapsed,
        "rows": rows,
    }


def _load_node_models(run_dir: Path, plan: Dict[str, Any]) -> Dict[str, str]:
    models: Dict[str, str] = {}
    for node in plan.get("nodes", []):
        name = node["name"]
        agent_file = node.get("agent_file")
        raw_model = None
        if agent_file:
            for candidate in agent_path_candidates(run_dir, agent_file):
                raw_model = read_agent_frontmatter(candidate).get("model")
                if raw_model:
                    break
        models[name] = normalize_model_label(raw_model)
    return models


def _build_node_artifacts(run_dir: Path, plan: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    artifacts: Dict[str, Dict[str, Any]] = {}
    if not plan:
        return artifacts

    for node in plan.get("nodes", []):
        entries = []
        for output in node.get("outputs", []):
            output_path = Path(output)
            resolved = output_path if output_path.is_absolute() else (run_dir / output_path)
            exists = resolved.exists()
            size_bytes = None
            if exists:
                try:
                    size_bytes = resolved.stat().st_size
                except OSError:
                    size_bytes = None
            entries.append(
                {
                    "path": str(output),
                    "absolutePath": str(resolved),
                    "exists": exists,
                    "sizeBytes": size_bytes,
                }
            )
        artifacts[node["name"]] = {"outputs": entries}
    return artifacts


def _resolve_final_output(run_dir: Path, plan: Optional[Dict[str, Any]], status: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    relative_path = None
    if status:
        relative_path = status.get("final_output")
    if not relative_path and plan:
        relative_path = plan.get("final_output")
    if not relative_path:
        return None

    output_path = Path(relative_path)
    resolved = output_path if output_path.is_absolute() else (run_dir / output_path)
    exists = resolved.exists()
    size_bytes = None
    if exists:
        try:
            size_bytes = resolved.stat().st_size
        except OSError:
            size_bytes = None

    return {
        "relativePath": str(relative_path),
        "absolutePath": str(resolved),
        "exists": exists,
        "sizeBytes": size_bytes,
    }


def _apply_staleness(status: Optional[Dict[str, Any]], status_path: Path) -> tuple[Optional[Dict[str, Any]], bool, float]:
    if status is None:
        return None, False, 0.0

    updated_at = _parse_iso(status.get("updated_at"))
    if updated_at is None:
        return status, False, 0.0

    now = datetime.now(timezone.utc).astimezone()
    stale_seconds = max((now - updated_at).total_seconds(), 0.0)

    try:
        file_age = max(time.time() - status_path.stat().st_mtime, 0.0)
        stale_seconds = max(stale_seconds, file_age)
    except OSError:
        pass

    if stale_seconds < STALE_THRESHOLD_S:
        return status, False, stale_seconds

    display_status = copy.deepcopy(status)
    running_nodes = 0
    for node_data in display_status.get("nodes", {}).values():
        if node_data.get("state") == "running":
            running_nodes += 1
            node_data["state"] = "failed"

    if running_nodes and display_status.get("state") not in {"completed", "failed"}:
        display_status["state"] = "failed"
        display_status["activity"] = "Orchestrator unresponsive (status stale)"

    return display_status, running_nodes > 0, stale_seconds


def _relative_to_run_dir(run_dir: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (run_dir / candidate).resolve()
    resolved.relative_to(run_dir)
    return resolved


def _parse_geometry(geometry: Optional[str]) -> Dict[str, Optional[int]]:
    if not geometry:
        return {"width": None, "height": None, "x": None, "y": None}

    match = re.match(r"^(?P<width>\d+)x(?P<height>\d+)(?:\+(?P<x>-?\d+)\+(?P<y>-?\d+))?$", geometry)
    if not match:
        return {"width": None, "height": None, "x": None, "y": None}

    groups = match.groupdict()
    return {
        "width": int(groups["width"]) if groups["width"] else None,
        "height": int(groups["height"]) if groups["height"] else None,
        "x": int(groups["x"]) if groups["x"] else None,
        "y": int(groups["y"]) if groups["y"] else None,
    }


def _find_chromium_browser() -> Optional[Path]:
    env = os.environ
    candidates = [
        Path(env.get("PROGRAMFILES(X86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(env.get("PROGRAMFILES", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(env.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(env.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(env.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(env.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


class GraphMonitorService:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir.resolve()
        self.plan_path = self.run_dir / "execution_plan.json"
        self.status_path = self.run_dir / "logs" / "status.json"
        self.run_status_path = self.run_dir / "logs" / "run-status.md"
        self.timeline_path = self.run_dir / "logs" / "timeline.jsonl"
        self.exit_signal_path = self.run_dir / "logs" / "_save_and_exit"

    def build_snapshot(self) -> Dict[str, Any]:
        plan = _read_json(self.plan_path)
        raw_status = _read_json(self.status_path)
        status, stale, stale_seconds = _apply_staleness(raw_status, self.status_path)

        run_status = None
        if self.run_status_path.exists():
            try:
                run_status = _parse_run_status(self.run_status_path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                run_status = None

        timeline = _read_timeline(self.timeline_path) if self.timeline_path.exists() else []
        node_models = _load_node_models(self.run_dir, plan) if plan else {}
        node_artifacts = _build_node_artifacts(self.run_dir, plan)
        final_output = _resolve_final_output(self.run_dir, plan, raw_status or status)

        if run_status and timeline:
            data_mode = "sidecar"
        elif status:
            data_mode = "status-only"
        elif plan:
            data_mode = "plan-only"
        else:
            data_mode = "waiting"

        terminal = bool(status and status.get("state") in {"completed", "failed"})

        return {
            "snapshotTakenAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "runDir": str(self.run_dir),
            "plan": plan,
            "status": status,
            "runStatus": run_status,
            "timeline": timeline,
            "nodeModels": node_models,
            "nodeArtifacts": node_artifacts,
            "finalOutput": final_output,
            "meta": {
                "dataMode": data_mode,
                "stale": stale,
                "staleSeconds": round(stale_seconds, 1),
                "terminal": terminal,
                "exitSignalSeen": self.exit_signal_path.exists(),
                "planMtimeNs": _path_stats(self.plan_path)["mtimeNs"],
                "statusMtimeNs": _path_stats(self.status_path)["mtimeNs"],
                "runStatusMtimeNs": _path_stats(self.run_status_path)["mtimeNs"],
                "timelineMtimeNs": _path_stats(self.timeline_path)["mtimeNs"],
            },
        }

    def load_preview(self, raw_path: str) -> Dict[str, Any]:
        resolved = _relative_to_run_dir(self.run_dir, raw_path)
        if not resolved.is_file():
            raise FileNotFoundError(raw_path)

        content = resolved.read_text(encoding="utf-8", errors="replace")
        truncated = len(content) > PREVIEW_CHAR_LIMIT
        if truncated:
            content = content[:PREVIEW_CHAR_LIMIT] + "\n\n[Preview truncated]"

        try:
            relative = str(resolved.relative_to(self.run_dir))
        except ValueError:
            relative = str(resolved)

        return {
            "path": relative,
            "absolutePath": str(resolved),
            "content": content,
            "truncated": truncated,
        }


class GraphMonitorRequestHandler(BaseHTTPRequestHandler):
    server_version = "multi-agent-graph-monitor/2.0"

    @property
    def service(self) -> GraphMonitorService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/api/snapshot":
            self._send_json(200, self.service.build_snapshot())
            return

        if parsed.path == "/api/file":
            query = parse_qs(parsed.query)
            raw_path = (query.get("path") or [""])[0]
            if not raw_path:
                self._send_json(400, {"error": "Missing `path` query parameter"})
                return
            try:
                payload = self.service.load_preview(raw_path)
            except ValueError:
                self._send_json(403, {"error": "Preview path must stay inside the run directory"})
                return
            except FileNotFoundError:
                self._send_json(404, {"error": "Preview file not found"})
                return
            except OSError as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, payload)
            return

        self._serve_static(parsed.path)

    def _serve_static(self, request_path: str) -> None:
        if WEB_APP_DIST.is_dir():
            relative = request_path.lstrip("/") or "index.html"
            candidate = (WEB_APP_DIST / relative).resolve()
            try:
                candidate.relative_to(WEB_APP_DIST)
            except ValueError:
                self._send_text(403, "Forbidden")
                return

            if not candidate.is_file():
                candidate = WEB_APP_DIST / "index.html"

            try:
                body = candidate.read_bytes()
            except OSError:
                self._send_text(404, "Not found")
                return

            mime_type, _ = mimetypes.guess_type(candidate.name)
            self.send_response(200)
            self.send_header("Content-Type", mime_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        fallback = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>multi-agent-graph monitor</title>
  <style>
    body {{ font-family: Segoe UI, sans-serif; background:#f8fafc; color:#0f172a; padding:32px; }}
    code {{ background:#e2e8f0; padding:2px 6px; border-radius:6px; }}
  </style>
</head>
<body>
  <h1>Frontend build missing</h1>
  <p>The React monitor assets were not found at <code>{WEB_APP_DIST}</code>.</p>
  <p>Build them with:</p>
  <pre>cd "{PLUGIN_ROOT / "web" / "graph-monitor"}"
npm install
npm run build</pre>
</body>
</html>"""
        self._send_text(200, fallback, "text/html; charset=utf-8")

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, payload: str, content_type: str = "text/plain; charset=utf-8") -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class GraphMonitorApp:
    def __init__(self, run_dir: Path, geometry: Optional[str] = None, open_browser: bool = True):
        self.service = GraphMonitorService(run_dir)
        self.geometry = _parse_geometry(geometry)
        self.open_browser = open_browser
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), GraphMonitorRequestHandler)
        self.server.service = self.service  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def _launch_browser(self) -> None:
        browser_path = _find_chromium_browser()
        width = self.geometry["width"]
        height = self.geometry["height"]
        x = self.geometry["x"]
        y = self.geometry["y"]

        if browser_path is not None:
            cmd = [str(browser_path), f"--app={self.url}"]
            if width and height:
                cmd.append(f"--window-size={width},{height}")
            if x is not None and y is not None:
                cmd.append(f"--window-position={x},{y}")
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass

        webbrowser.open(self.url, new=1)

    def _write_final_snapshot(self) -> None:
        out_path = self.service.run_dir / "run-final-state.json"
        try:
            out_path.write_text(json.dumps(self.service.build_snapshot(), indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def run(self) -> None:
        self.thread.start()

        if self.open_browser:
            self._launch_browser()

        try:
            while True:
                if self.service.exit_signal_path.exists():
                    self._write_final_snapshot()
                    self.service.exit_signal_path.unlink(missing_ok=True)
                    break
                time.sleep(POLL_INTERVAL_S)
        except KeyboardInterrupt:
            pass
        finally:
            self.server.shutdown()
            self.server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Web graph monitor for multi-agent-graph runs")
    parser.add_argument("run_dir", help="Path to the run directory")
    parser.add_argument("--geometry", default=None, help="Window geometry (WxH+X+Y) for Chromium app mode")
    parser.add_argument("--no-browser", action="store_true", help="Serve the monitor without opening a browser window")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"Error: '{run_dir}' is not a directory", file=sys.stderr)
        sys.exit(1)

    app = GraphMonitorApp(run_dir, geometry=args.geometry, open_browser=not args.no_browser)
    app.run()


if __name__ == "__main__":
    main()
