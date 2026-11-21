"""Register a generated server with Claude Code."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from web_shooter.codegen import GeneratedServer

Scope = Literal["project", "user"]


def server_entry(server: GeneratedServer) -> dict[str, object]:
    """How Claude Code should launch the server.

    With uv installed, `uvx --from <dir>` runs the generated package with its own
    dependencies, independent of web-shooter's environment. Otherwise reuse the current
    interpreter (which has mcp, httpx and jsonschema) with the package on PYTHONPATH.
    """
    if shutil.which("uvx"):
        return {
            "command": "uvx",
            "args": ["--from", str(server.root.resolve()), server.dist_name],
            "env": {},
        }
    return {
        "command": sys.executable,
        "args": ["-m", server.package],
        "env": {"PYTHONPATH": str(server.src.resolve())},
    }


def claude_add_command(server: GeneratedServer, scope: Scope) -> list[str]:
    entry = server_entry(server)
    env = entry["env"]
    assert isinstance(env, dict)
    cmd = ["claude", "mcp", "add", "--scope", scope]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    args = entry["args"]
    assert isinstance(args, list)
    return [*cmd, server.dist_name, "--", str(entry["command"]), *args]


def write_mcp_json(server: GeneratedServer, project_dir: Path) -> Path:
    path = project_dir / ".mcp.json"
    data: dict[str, object] = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    servers = data.setdefault("mcpServers", {})
    assert isinstance(servers, dict)
    servers[server.dist_name] = server_entry(server)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def install(
    server: GeneratedServer, scope: Scope = "project", project_dir: Path | None = None
) -> str:
    """Use the `claude` CLI when available; otherwise write .mcp.json (project scope)."""
    if shutil.which("claude"):
        result = subprocess.run(
            claude_add_command(server, scope),
            capture_output=True,
            text=True,
            cwd=project_dir,
            timeout=60,
        )
        if result.returncode == 0:
            return f"registered with `claude mcp add` ({scope} scope)"
        if scope == "user":
            raise RuntimeError(f"claude mcp add failed: {result.stderr.strip()}")
    path = write_mcp_json(server, project_dir or Path.cwd())
    return f"written to {path}"
