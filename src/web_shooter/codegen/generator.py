"""Render a standalone, installable MCP server package from an APISpec + ToolPlan."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from web_shooter.ir import APISpec
from web_shooter.tools import ToolPlan, snake

TEMPLATES = Path(__file__).with_name("templates")
RUNTIME = Path(__file__).with_name("runtime.py")


@dataclass
class GeneratedServer:
    root: Path
    package: str
    dist_name: str
    env_prefix: str

    @property
    def src(self) -> Path:
        return self.root / "src"

    @property
    def package_dir(self) -> Path:
        return self.src / self.package

    @property
    def config_path(self) -> Path:
        return self.package_dir / "tools.json"

    def config(self) -> dict[str, Any]:
        return json.loads(self.config_path.read_text(encoding="utf-8"))


def names_for(title: str) -> tuple[str, str, str]:
    """Return (python package, distribution name, env var prefix) for an API title."""
    base = snake(re.sub(r"\b(api|rest|openapi|swagger)\b", "", title, flags=re.I)) or "api"
    base = base[:40].strip("_") or "api"
    if base[0].isdigit():
        base = f"api_{base}"
    package = f"{base}_mcp"
    return package, package.replace("_", "-"), base.upper()


def build_config(
    spec: APISpec, plan: ToolPlan, server_name: str, env_prefix: str
) -> dict[str, Any]:
    auth = []
    for s in spec.auth_schemes:
        auth.append(
            {
                "name": s.name,
                "type": s.type.value,
                "param_name": s.param_name,
                "location": s.location.value if s.location else None,
                "env_var": f"{env_prefix}_{snake(s.name).upper()}",
            }
        )
    return {
        "server_name": server_name,
        "title": spec.title,
        "base_url": spec.base_url,
        "env_prefix": env_prefix,
        "auth": auth,
        "tools": [t.model_dump(mode="json") for t in plan.tools],
        "skipped": plan.skipped,
    }


def generate(
    spec: APISpec, plan: ToolPlan, out_dir: Path, package: str | None = None
) -> GeneratedServer:
    pkg, dist, prefix = names_for(spec.title)
    if package:
        pkg, dist = package, package.replace("_", "-")
    root = out_dir / dist
    pkg_dir = root / "src" / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)

    config = build_config(spec, plan, dist, prefix)
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )

    def safe(text: str) -> str:
        # Values end up inside a Python docstring: no backslashes, no closing quotes.
        return text.replace("\\", "/").replace('"""', "'''")

    context = {
        "title": safe(spec.title),
        "description": spec.description.strip().split("\n\n")[0][:500],
        "origin": safe(spec.origin),
        "base_url": spec.base_url,
        "package": pkg,
        "dist_name": dist,
        "env_prefix": prefix,
        "auth": config["auth"],
        "tools": [
            {"name": t["name"], "summary": t["description"].split("\n")[0].replace("|", "/")}
            for t in config["tools"]
        ],
        "skipped": plan.skipped,
    }
    files = {
        root / "pyproject.toml": "pyproject.toml.j2",
        root / "README.md": "README.md.j2",
        root / "Dockerfile": "Dockerfile.j2",
        pkg_dir / "server.py": "server.py.j2",
        pkg_dir / "__main__.py": "__main__.py.j2",
    }
    for path, template in files.items():
        path.write_text(env.get_template(template).render(**context), encoding="utf-8")
    (pkg_dir / "__init__.py").write_text(f'"""{spec.title} MCP server."""\n', encoding="utf-8")
    (pkg_dir / "_runtime.py").write_text(RUNTIME.read_text(encoding="utf-8"), encoding="utf-8")
    (pkg_dir / "tools.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (root / "api_spec.json").write_text(spec.to_json(), encoding="utf-8")
    return GeneratedServer(root=root, package=pkg, dist_name=dist, env_prefix=prefix)
