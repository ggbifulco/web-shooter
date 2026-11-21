"""Start a generated server in isolation and hand back MCP stdio parameters.

Two modes:
- local:  a subprocess of the current Python interpreter (fast, used by tests).
- docker: an image built from the generated Dockerfile, run with resource limits.
          In mock mode the container has no network at all. In live mode it gets the
          default bridge network and the runtime itself refuses any host other than
          the API's base URL host (app-level allowlist).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Literal

from mcp.client.stdio import StdioServerParameters

from web_shooter.codegen import GeneratedServer


class SandboxError(RuntimeError):
    pass


@dataclass
class SandboxConfig:
    mode: Literal["local", "docker"] = "local"
    mock: bool = True
    env: dict[str, str] = field(default_factory=dict)
    call_timeout: float = 30.0
    suite_timeout: float = 300.0
    memory: str = "512m"
    cpus: str = "1"
    pids: int = 128


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def image_tag(server: GeneratedServer) -> str:
    return f"web-shooter/{server.dist_name}:latest"


def build_image(server: GeneratedServer, timeout: float = 600) -> str:
    tag = image_tag(server)
    result = subprocess.run(
        ["docker", "build", "-q", "-t", tag, str(server.root)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise SandboxError(f"docker build failed:\n{result.stderr[-3000:]}")
    return tag


def _server_env(server: GeneratedServer, cfg: SandboxConfig) -> dict[str, str]:
    env = dict(cfg.env)
    if cfg.mock:
        env["WEB_SHOOTER_MOCK"] = "1"
    return env


def server_params(server: GeneratedServer, cfg: SandboxConfig) -> StdioServerParameters:
    env = _server_env(server, cfg)
    if cfg.mode == "local":
        base = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP")}
        base["PYTHONPATH"] = str(server.src.resolve())
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", server.package],
            env={**base, **env},
            cwd=str(server.root.resolve()),
        )
    tag = build_image(server)
    args = [
        "run",
        "-i",
        "--rm",
        "--network",
        "none" if cfg.mock else "bridge",
        "--memory",
        cfg.memory,
        "--cpus",
        cfg.cpus,
        "--pids-limit",
        str(cfg.pids),
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
    ]
    for key in env:
        args += ["-e", key]
    args.append(tag)
    # docker reads the values of `-e KEY` from its own environment.
    return StdioServerParameters(command="docker", args=args, env={**os.environ, **env})
