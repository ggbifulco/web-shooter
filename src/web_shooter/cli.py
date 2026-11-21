"""Command line interface: `web-shooter shoot <docs-url-or-spec>`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from web_shooter import __version__
from web_shooter.codegen.generator import GeneratedServer
from web_shooter.ingest import SpecError
from web_shooter.llm import LLM, AnthropicLLM, default_llm
from web_shooter.pipeline import ShootOptions, plan_only, shoot
from web_shooter.sandbox import SandboxConfig, docker_available
from web_shooter.testing import run_tests


def _print(msg: str) -> None:
    print(msg, flush=True)


def _llm(args: argparse.Namespace) -> LLM | None:
    if args.no_llm:
        return None
    if args.model:
        return AnthropicLLM(model=args.model)
    return default_llm()


def _sandbox(args: argparse.Namespace) -> SandboxConfig:
    if args.sandbox == "docker" and not docker_available():
        raise SystemExit("error: --sandbox docker needs a running Docker daemon")
    return SandboxConfig(mode=args.sandbox, mock=not args.live)


def cmd_shoot(args: argparse.Namespace) -> int:
    llm = _llm(args)
    opts = ShootOptions(
        out_dir=Path(args.out),
        sandbox=_sandbox(args),
        include_destructive=args.include_destructive,
        consolidate=args.consolidate,
        polish=args.polish,
        repair=not args.no_repair,
        max_repairs=args.max_repairs,
        max_pages=args.max_pages,
        min_confidence=args.min_confidence,
        package=args.package,
    )
    if args.dry_run:
        result = plan_only(args.source, opts, llm)
        _print(
            f"{result.spec.title}: {len(result.spec.endpoints)} endpoints"
            f" -> {len(result.plan.tools)} tools"
        )
        for t in result.plan.tools:
            ops = ", ".join(f"{o.method.upper()} {o.path}" for o in t.operations)
            _print(f"  {t.name:40} {ops}")
        for s in result.plan.skipped:
            _print(f"  skipped: {s}")
        return 0

    result = shoot(args.source, opts, llm=llm, progress=_print)
    assert result.server is not None and result.report is not None
    if llm is not None:
        cost = llm.usage.cost_usd(llm.model)
        _print(
            f"LLM: {llm.usage.calls} calls, "
            f"{llm.usage.input_tokens}+{llm.usage.output_tokens} tokens, ~${cost:.3f}"
        )
    if args.install_claude:
        if not result.ok:
            _print("not registering with Claude Code: tests are failing")
        else:
            from web_shooter.install import install

            _print("claude: " + install(result.server, scope=args.install_claude))
    if args.json:
        _print(
            json.dumps(
                {
                    "server": str(result.server.root),
                    "tools": len(result.plan.tools),
                    "ok": result.ok,
                    "summary": result.report.summary(),
                    "timings": {k: round(v, 2) for k, v in result.timings.items()},
                }
            )
        )
    status = "ready" if result.ok else "generated, but some tests fail"
    _print(f"\n{result.server.dist_name}: {status} -> {result.server.root}")
    return 0 if result.ok else 1


def _load_server(path: Path) -> GeneratedServer:
    src = path / "src"
    packages = [p for p in src.iterdir() if (p / "tools.json").exists()] if src.exists() else []
    if not packages:
        raise SystemExit(f"error: {path} is not a generated server (no src/*/tools.json)")
    pkg = packages[0].name
    config = json.loads((packages[0] / "tools.json").read_text(encoding="utf-8"))
    return GeneratedServer(
        root=path, package=pkg, dist_name=path.name, env_prefix=config["env_prefix"]
    )


def cmd_test(args: argparse.Namespace) -> int:
    server = _load_server(Path(args.path))
    report = run_tests(server, _sandbox(args))
    for r in report.results:
        mark = {"passed": "ok  ", "failed": "FAIL", "inconclusive": "??  "}[r.status]
        _print(f"{mark} {r.id}" + (f"  {r.detail[:150]}" if r.detail else ""))
    _print(report.summary())
    return 0 if report.ok else 1


def cmd_install(args: argparse.Namespace) -> int:
    from web_shooter.install import install

    _print(install(_load_server(Path(args.path)), scope=args.scope))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="web-shooter", description=__doc__)
    p.add_argument("--version", action="version", version=f"web-shooter {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def sandbox_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--sandbox", choices=["local", "docker"], default="local")
        sp.add_argument(
            "--live", action="store_true", help="call the real API in tests (default: mock)"
        )

    s = sub.add_parser("shoot", help="generate a tested MCP server from a spec or docs URL")
    s.add_argument("source", help="OpenAPI/Swagger file or URL, or an HTML docs URL")
    s.add_argument("--out", default="out", help="output directory (default: out)")
    s.add_argument("--model", help="Claude model id (default: claude-opus-5-5)")
    s.add_argument("--no-llm", action="store_true", help="never call an LLM")
    s.add_argument("--dry-run", action="store_true", help="only show the planned tools")
    s.add_argument("--package", help="python package name of the generated server")
    s.add_argument(
        "--include-destructive", action="store_true", help="also expose DELETE endpoints"
    )
    s.add_argument("--consolidate", action="store_true", help="merge CRUD endpoints per resource")
    s.add_argument("--polish", action="store_true", help="rewrite tool descriptions with the LLM")
    s.add_argument("--no-repair", action="store_true", help="disable the self-repair loop")
    s.add_argument("--max-repairs", type=int, default=3)
    s.add_argument("--max-pages", type=int, default=30, help="docs pages to crawl")
    s.add_argument("--min-confidence", type=float, default=0.0)
    s.add_argument("--install-claude", choices=["project", "user"], help="register in Claude Code")
    s.add_argument("--json", action="store_true", help="print a JSON summary at the end")
    sandbox_args(s)
    s.set_defaults(func=cmd_shoot)

    t = sub.add_parser("test", help="re-run the generated tests of a server")
    t.add_argument("path")
    sandbox_args(t)
    t.set_defaults(func=cmd_test)

    i = sub.add_parser("install", help="register a generated server in Claude Code")
    i.add_argument("path")
    i.add_argument("--scope", choices=["project", "user"], default="project")
    i.set_defaults(func=cmd_install)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SpecError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
