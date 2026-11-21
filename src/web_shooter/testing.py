"""Generate tests for every tool and run them against a server over MCP."""

from __future__ import annotations

import copy
import json
from typing import Any, Literal

import anyio
from jsonschema import ValidationError
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from mcp import Client
from pydantic import BaseModel, Field

from web_shooter.codegen import GeneratedServer
from web_shooter.codegen.runtime import sample_from_schema
from web_shooter.sandbox import SandboxConfig, server_params

CaseKind = Literal["valid", "missing_required", "wrong_type"]
Status = Literal["passed", "failed", "inconclusive"]
# In live mode these mean "the API answered but rejected our made-up inputs":
# the tool works, the test data was not realistic enough to tell more.
INCONCLUSIVE_HTTP = ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404", "HTTP 409", "HTTP 422")


class TestCase(BaseModel):
    __test__ = False  # not a pytest class

    id: str
    tool: str
    kind: CaseKind
    arguments: dict[str, Any]
    expect_error: bool
    response_schema: dict[str, Any] | None = None


class CaseResult(BaseModel):
    id: str
    tool: str
    kind: CaseKind
    status: Status
    detail: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    response_excerpt: str = ""


class TestReport(BaseModel):
    __test__ = False

    listing_ok: bool = True
    listing_detail: str = ""
    results: list[CaseResult] = Field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(r.status == "passed" for r in self.results)

    @property
    def failed(self) -> list[CaseResult]:
        return [r for r in self.results if r.status == "failed"]

    @property
    def inconclusive(self) -> int:
        return sum(r.status == "inconclusive" for r in self.results)

    @property
    def ok(self) -> bool:
        return self.listing_ok and not self.failed

    def summary(self) -> str:
        return (
            f"{self.passed} passed, {len(self.failed)} failed, {self.inconclusive} inconclusive"
            + ("" if self.listing_ok else f" | tool listing: {self.listing_detail}")
        )


# ------------------------------------------------------------------ generation


def _wrong_value(schema: dict[str, Any]) -> Any:
    t = schema.get("type")
    return {"string": 12345, "integer": "not-a-number", "number": "NaN?", "boolean": "maybe"}.get(
        t if isinstance(t, str) else ""
    )


def generate_tests(config: dict[str, Any]) -> list[TestCase]:
    cases: list[TestCase] = []
    for tool in config["tools"]:
        schema = tool["input_schema"]
        props: dict[str, Any] = schema.get("properties", {})
        for op in tool["operations"]:
            label = f"{tool['name']}" + (f"[{op['action']}]" if op.get("action") else "")
            args: dict[str, Any] = {}
            for name in op.get("required_args", []):
                args[name] = sample_from_schema(props.get(name, {"type": "string"}))
            if op.get("action"):
                args["action"] = op["action"]
            cases.append(
                TestCase(
                    id=f"{label}:valid",
                    tool=tool["name"],
                    kind="valid",
                    arguments=args,
                    expect_error=False,
                    response_schema=op.get("response_schema"),
                )
            )
            required = [r for r in op.get("required_args", []) if r != "action"]
            if required:
                broken = {k: v for k, v in args.items() if k != required[0]}
                cases.append(
                    TestCase(
                        id=f"{label}:missing_required",
                        tool=tool["name"],
                        kind="missing_required",
                        arguments=broken,
                        expect_error=True,
                    )
                )
        typed = next(
            ((k, v) for k, v in props.items() if k != "action" and _wrong_value(v) is not None),
            None,
        )
        if typed:
            name, prop_schema = typed
            first_op = tool["operations"][0]
            args = {n: sample_from_schema(props[n]) for n in first_op.get("required_args", [])}
            if first_op.get("action"):
                args["action"] = first_op["action"]
            args[name] = _wrong_value(prop_schema)
            cases.append(
                TestCase(
                    id=f"{tool['name']}:wrong_type",
                    tool=tool["name"],
                    kind="wrong_type",
                    arguments=args,
                    expect_error=True,
                )
            )
    return cases


# ------------------------------------------------------------------ execution


def _strip_patterns(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip_patterns(v) for k, v in schema.items() if k != "pattern"}
    if isinstance(schema, list):
        return [_strip_patterns(v) for v in schema]
    return schema


def validate_response(text: str, schema: dict[str, Any] | None) -> str | None:
    """Return an error message, or None if the response matches the declared schema."""
    if not schema:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None if schema.get("type") == "string" else "response is not JSON"
    schema = _strip_patterns(copy.deepcopy(schema))
    try:
        cls = validator_for(schema)
        cls(schema).validate(data)
    except ValidationError as e:
        path = "/".join(str(p) for p in e.absolute_path) or "<root>"
        return f"response does not match schema at {path}: {e.message[:300]}"
    except SchemaError:
        return None
    return None


def check_listing(tools: list[Any], config: dict[str, Any]) -> str | None:
    listed = {t.name: t for t in tools}
    expected = {t["name"] for t in config["tools"]}
    if missing := expected - set(listed):
        return f"tools missing from listing: {sorted(missing)}"
    for t in listed.values():
        try:
            validator_for(t.input_schema).check_schema(t.input_schema)
        except SchemaError as e:
            return f"invalid input schema for {t.name}: {e.message[:200]}"
    return None


async def run_suite_async(
    server: GeneratedServer, cases: list[TestCase], cfg: SandboxConfig
) -> TestReport:
    config = server.config()
    report = TestReport()
    params = server_params(server, cfg)
    with anyio.fail_after(cfg.suite_timeout):
        async with Client(params, read_timeout_seconds=cfg.call_timeout) as client:
            listing = await client.list_tools()
            problem = check_listing(listing.tools, config)
            if problem:
                report.listing_ok, report.listing_detail = False, problem
            for case in cases:
                report.results.append(await _run_case(client, case, cfg))
    return report


async def _run_case(client: Client, case: TestCase, cfg: SandboxConfig) -> CaseResult:
    base: dict[str, Any] = {
        "id": case.id,
        "tool": case.tool,
        "kind": case.kind,
        "arguments": case.arguments,
    }
    try:
        result = await client.call_tool(case.tool, case.arguments)
    except Exception as e:  # transport errors, timeouts, crashed server
        return CaseResult(**base, status="failed", detail=f"call raised {type(e).__name__}: {e}")
    text = "\n".join(getattr(c, "text", "") for c in result.content)
    excerpt = text[:500]
    if case.expect_error:
        if result.is_error:
            return CaseResult(**base, status="passed", response_excerpt=excerpt)
        return CaseResult(
            **base,
            status="failed",
            detail="expected an error for invalid input",
            response_excerpt=excerpt,
        )
    if result.is_error:
        if not cfg.mock and text.startswith(INCONCLUSIVE_HTTP):
            return CaseResult(
                **base, status="inconclusive", detail=text[:200], response_excerpt=excerpt
            )
        return CaseResult(**base, status="failed", detail=text[:1000], response_excerpt=excerpt)
    problem = validate_response(text, case.response_schema)
    if problem:
        return CaseResult(**base, status="failed", detail=problem, response_excerpt=excerpt)
    return CaseResult(**base, status="passed", response_excerpt=excerpt)


def run_tests(
    server: GeneratedServer, cfg: SandboxConfig, cases: list[TestCase] | None = None
) -> TestReport:
    cases = cases if cases is not None else generate_tests(server.config())

    async def main() -> TestReport:
        return await run_suite_async(server, cases, cfg)

    try:
        return anyio.run(main)
    except Exception as e:  # server failed to start, suite timeout, ...
        return TestReport(listing_ok=False, listing_detail=f"{type(e).__name__}: {_root_cause(e)}")


def _root_cause(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return str(e)[:1000]
