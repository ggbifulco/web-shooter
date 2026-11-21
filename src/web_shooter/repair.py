"""Self-repair: feed failing tests back to an LLM and apply its search/replace patches."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from web_shooter.codegen import GeneratedServer
from web_shooter.llm import LLM
from web_shooter.testing import TestReport

EditableFile = Literal["tools.json", "_runtime.py", "server.py"]
MAX_FILE_CHARS = 60_000


class Edit(BaseModel):
    file: EditableFile
    old: str = Field(description="Exact text currently in the file; must occur exactly once")
    new: str = Field(description="Replacement text")


class RepairPlan(BaseModel):
    analysis: str = Field(description="Root cause of the failures, in one or two sentences")
    edits: list[Edit]


class Iteration(BaseModel):
    index: int
    failures_before: int
    analysis: str = ""
    edits_applied: int = 0
    edit_errors: list[str] = Field(default_factory=list)
    failures_after: int | None = None
    seconds: float = 0.0


class RepairLog(BaseModel):
    iterations: list[Iteration] = Field(default_factory=list)
    final_ok: bool = False

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")


REPAIR_SYSTEM = (
    "You fix a generated MCP server that wraps an HTTP API. The server is data-driven: "
    "tools.json describes tools, argument bindings and schemas; _runtime.py turns tool "
    "calls into HTTP requests. You get failing test results and the current files. "
    "Return minimal search/replace edits. Each 'old' must be copied exactly from the file "
    "and be unique in it. Prefer fixing tools.json (schemas, bindings, paths) over the "
    "runtime. Never weaken a test expectation by making invalid input succeed."
)


def apply_edit(server: GeneratedServer, edit: Edit) -> str | None:
    """Apply one edit; return an error message instead of raising."""
    path = server.package_dir / edit.file
    if not path.exists():
        return f"{edit.file}: file does not exist"
    text = path.read_text(encoding="utf-8")
    count = text.count(edit.old) if edit.old else 0
    if count != 1:
        return f"{edit.file}: 'old' text found {count} times (must be exactly once)"
    updated = text.replace(edit.old, edit.new, 1)
    if edit.file == "tools.json":
        try:
            json.loads(updated)
        except json.JSONDecodeError as e:
            return f"tools.json: edit would make invalid JSON ({e})"
    path.write_text(updated, encoding="utf-8")
    return None


def _failure_report(report: TestReport, limit: int = 15) -> str:
    lines = []
    if not report.listing_ok:
        lines.append(f"- server/listing problem: {report.listing_detail}")
    for r in report.failed[:limit]:
        lines.append(
            f"- {r.id} ({r.kind})\n  arguments: {json.dumps(r.arguments)[:400]}\n"
            f"  problem: {r.detail[:600]}\n  response: {r.response_excerpt[:300]}"
        )
    return "\n".join(lines)


def build_prompt(server: GeneratedServer, report: TestReport) -> str:
    parts = [f"Failing tests:\n{_failure_report(report)}\n"]
    for name in ("tools.json", "_runtime.py"):
        text = (server.package_dir / name).read_text(encoding="utf-8")
        parts.append(f'<file name="{name}">\n{text[:MAX_FILE_CHARS]}\n</file>')
    return "\n\n".join(parts)


def repair_loop(
    server: GeneratedServer,
    run: Callable[[], TestReport],
    llm: LLM,
    max_iterations: int = 3,
    log_path: Path | None = None,
    initial: TestReport | None = None,
) -> tuple[TestReport, RepairLog]:
    """Run tests, ask the LLM for patches while they fail, up to max_iterations."""
    log = RepairLog()
    report = initial or run()
    for i in range(1, max_iterations + 1):
        if report.ok:
            break
        start = time.monotonic()
        it = Iteration(
            index=i, failures_before=len(report.failed) + (0 if report.listing_ok else 1)
        )
        plan = llm.generate(
            system=REPAIR_SYSTEM, prompt=build_prompt(server, report), output=RepairPlan
        )
        it.analysis = plan.analysis
        for edit in plan.edits:
            error = apply_edit(server, edit)
            if error:
                it.edit_errors.append(error)
            else:
                it.edits_applied += 1
        report = run()
        it.failures_after = len(report.failed) + (0 if report.listing_ok else 1)
        it.seconds = round(time.monotonic() - start, 2)
        log.iterations.append(it)
        if log_path:
            log.save(log_path)
    log.final_ok = report.ok
    if log_path:
        log.save(log_path)
    return report, log
