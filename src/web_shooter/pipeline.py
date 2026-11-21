"""End-to-end flow: ingest -> design tools -> generate -> test -> repair."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from web_shooter.codegen import GeneratedServer, generate
from web_shooter.ingest import ingest
from web_shooter.ir import APISpec
from web_shooter.llm import LLM
from web_shooter.repair import RepairLog, repair_loop
from web_shooter.sandbox import SandboxConfig
from web_shooter.testing import TestReport, generate_tests, run_tests
from web_shooter.tools import ToolPlan, design_tools, polish_descriptions


@dataclass
class ShootOptions:
    out_dir: Path = Path("out")
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    include_destructive: bool = False
    consolidate: bool = False
    polish: bool = False
    repair: bool = True
    max_repairs: int = 3
    max_pages: int = 30
    min_confidence: float = 0.0
    package: str | None = None


@dataclass
class ShootResult:
    spec: APISpec
    plan: ToolPlan
    server: GeneratedServer | None = None
    report: TestReport | None = None
    repair_log: RepairLog | None = None
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.report is not None and self.report.ok


Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def plan_only(source: str, opts: ShootOptions, llm: LLM | None = None) -> ShootResult:
    spec = ingest(source, llm=llm, max_pages=opts.max_pages)
    plan = design_tools(
        spec,
        include_destructive=opts.include_destructive,
        consolidate=opts.consolidate,
        min_confidence=opts.min_confidence,
    )
    return ShootResult(spec=spec, plan=plan)


def shoot(
    source: str,
    opts: ShootOptions,
    llm: LLM | None = None,
    progress: Progress = _noop,
) -> ShootResult:
    timings: dict[str, float] = {}

    t = time.monotonic()
    progress(f"[1/5] ingest   {source}")
    result = plan_only(source, opts, llm)
    spec, plan = result.spec, result.plan
    timings["ingest"] = time.monotonic() - t
    progress(
        f"      {len(spec.endpoints)} endpoints, {len(plan.tools)} tools, "
        f"{len(plan.skipped)} skipped"
    )
    if not plan.tools:
        raise ValueError("no usable endpoints found")

    if opts.polish and llm is not None:
        t = time.monotonic()
        progress("[2/5] polish   tool descriptions")
        polish_descriptions(plan, llm)
        timings["polish"] = time.monotonic() - t
    else:
        progress("[2/5] polish   skipped")

    t = time.monotonic()
    server = generate(spec, plan, opts.out_dir, package=opts.package)
    timings["generate"] = time.monotonic() - t
    progress(f"[3/5] generate {server.root}")

    t = time.monotonic()
    mode = "mock" if opts.sandbox.mock else "live"
    progress(f"[4/5] test     {opts.sandbox.mode} sandbox, {mode} mode")
    cases = generate_tests(server.config())

    def run() -> TestReport:
        return run_tests(server, opts.sandbox, cases)

    report = run()
    timings["test"] = time.monotonic() - t
    progress(f"      {report.summary()}")

    log: RepairLog | None = None
    if not report.ok and opts.repair and llm is not None:
        t = time.monotonic()
        progress(f"[5/5] repair   up to {opts.max_repairs} iterations")
        report, log = repair_loop(
            server,
            run,
            llm,
            max_iterations=opts.max_repairs,
            log_path=server.root / "repair_log.json",
            initial=report,
        )
        timings["repair"] = time.monotonic() - t
        progress(f"      {report.summary()}")
    else:
        reason = (
            "all tests pass" if report.ok else "no LLM configured" if llm is None else "disabled"
        )
        progress(f"[5/5] repair   skipped ({reason})")

    (server.root / "test_report.json").write_text(
        json.dumps(
            {**report.model_dump(mode="json"), "ok": report.ok, "summary": report.summary()},
            indent=2,
        ),
        encoding="utf-8",
    )
    return ShootResult(
        spec=spec, plan=plan, server=server, report=report, repair_log=log, timings=timings
    )
