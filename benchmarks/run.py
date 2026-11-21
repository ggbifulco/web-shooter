"""Run web-shooter on every API in benchmarks/apis.yaml and record the results.

    uv run python benchmarks/run.py                 # mock-mode tests, LLM if a key is set
    uv run python benchmarks/run.py --only xkcd     # a single API
    uv run python benchmarks/run.py --update-readme # also rewrite the README table

Outputs benchmarks/results.json and benchmarks/results.md.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

import yaml

from web_shooter.llm import default_llm
from web_shooter.pipeline import ShootOptions, shoot
from web_shooter.sandbox import SandboxConfig

HERE = Path(__file__).parent
ROOT = HERE.parent
START, END = "<!-- benchmark:start -->", "<!-- benchmark:end -->"


def run_one(api: dict[str, str], out_dir: Path, model: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {"name": api["name"], "kind": api["kind"], "source": api["source"]}
    llm = default_llm(model)
    if api["kind"] == "docs" and llm is None:
        row["status"] = "needs LLM key"
        return row
    start = time.monotonic()
    opts = ShootOptions(
        out_dir=out_dir, sandbox=SandboxConfig(mode="local", mock=True, suite_timeout=900)
    )
    try:
        result = shoot(api["source"], opts, llm=llm)
    except Exception as e:  # record and continue: one broken API must not stop the run
        row.update(status="error", error=f"{type(e).__name__}: {str(e)[:200]}")
        row["seconds"] = round(time.monotonic() - start, 1)
        return row
    report = result.report
    assert report is not None
    row.update(
        status="ok" if result.ok else "failing",
        endpoints=len(result.spec.endpoints),
        tools=len(result.plan.tools),
        skipped=len(result.plan.skipped),
        tests=len(report.results),
        passed=report.passed,
        failed=len(report.failed),
        listing_ok=report.listing_ok,
        repair_iterations=len(result.repair_log.iterations) if result.repair_log else 0,
        llm_calls=llm.usage.calls if llm else 0,
        tokens=(llm.usage.input_tokens + llm.usage.output_tokens) if llm else 0,
        cost_usd=round(llm.usage.cost_usd(llm.model), 4) if llm else 0.0,
        seconds=round(time.monotonic() - start, 1),
    )
    if report.failed:
        row["first_failure"] = f"{report.failed[0].id}: {report.failed[0].detail[:160]}"
    if not report.listing_ok:
        row["first_failure"] = report.listing_detail[:160]
    return row


def to_markdown(rows: list[dict[str, Any]]) -> str:
    head = (
        "| API | Input | Endpoints | Tools | Tests passed | Repair iters "
        "| LLM cost | Time | Status |\n"
        "|---|---|---:|---:|---:|---:|---:|---:|---|\n"
    )
    lines = []
    for r in rows:
        if r["status"] in ("needs LLM key", "error"):
            detail = r.get("error", "")
            lines.append(
                f"| {r['name']} | {r['kind']} | - | - | - | - | - | - | {r['status']} {detail} |"
            )
            continue
        lines.append(
            f"| {r['name']} | {r['kind']} | {r['endpoints']} | {r['tools']} | "
            f"{r['passed']}/{r['tests']} | {r['repair_iterations']} | ${r['cost_usd']:.2f} | "
            f"{r['seconds']}s | {r['status']} |"
        )
    measured = [r for r in rows if "tests" in r]
    ok = sum(r["status"] == "ok" for r in measured)
    tests = sum(r["tests"] for r in measured)
    passed = sum(r["passed"] for r in measured)
    total = (
        f"\n**{ok}/{len(measured)} APIs fully green, {passed}/{tests} generated tests passing** "
        f"({len(rows) - len(measured)} not run).\n"
    )
    return head + "\n".join(lines) + "\n" + total


def update_readme(markdown: str) -> None:
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    block = f"{START}\n{markdown}{END}"
    text = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: block, text, flags=re.S)
    readme.write_text(text, encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--only", help="substring of an API name")
    p.add_argument("--model")
    p.add_argument("--update-readme", action="store_true")
    p.add_argument("--keep", action="store_true", help="keep generated servers")
    args = p.parse_args()

    apis = yaml.safe_load((HERE / "apis.yaml").read_text(encoding="utf-8"))["apis"]
    if args.only:
        apis = [a for a in apis if args.only.lower() in a["name"].lower()]
    out_dir = HERE / "out"
    rows = []
    for api in apis:
        row = run_one(api, out_dir, args.model)
        print(
            f"{row['name']:22} {row['status']:14} {row.get('passed', '-')}/{row.get('tests', '-')}"
        )
        rows.append(row)
    if not args.keep:
        shutil.rmtree(out_dir, ignore_errors=True)

    (HERE / "results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    markdown = to_markdown(rows)
    (HERE / "results.md").write_text(markdown, encoding="utf-8")
    if args.update_readme:
        update_readme(markdown)
    print(markdown)


if __name__ == "__main__":
    main()
