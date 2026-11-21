"""Tool design: decide which endpoints become MCP tools and how they look to an LLM."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, Field

from web_shooter.ir import APISpec, Endpoint
from web_shooter.llm import LLM

TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
PAGE_PARAMS = ("page", "page_number", "pageNumber", "offset")
MAX_DESCRIPTION = 1024

BindingLocation = Literal["path", "query", "header", "cookie", "body"]


class ArgBinding(BaseModel):
    """Maps a tool argument to where it goes in the HTTP request."""

    arg: str
    api_name: str
    location: BindingLocation


class Operation(BaseModel):
    action: str | None = None
    method: str
    path: str
    bindings: list[ArgBinding] = Field(default_factory=list)
    body_mode: Literal["none", "flat", "wrapped"] = "none"
    content_type: str = "application/json"
    response_schema: dict[str, Any] | None = None
    page_param: str | None = None
    required_args: list[str] = Field(default_factory=list)
    endpoint_key: str = ""


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    operations: list[Operation]
    destructive: bool = False


class ToolPlan(BaseModel):
    tools: list[ToolSpec]
    skipped: list[str] = Field(default_factory=list)


def snake(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text)
    return text.strip("_").lower()


def tool_name(endpoint: Endpoint) -> str:
    if endpoint.operation_id:
        name = snake(endpoint.operation_id)
    else:
        parts = []
        for seg in endpoint.path.strip("/").split("/"):
            if not seg:
                continue
            m = re.fullmatch(r"{(.+)}", seg)
            parts.append(f"by_{snake(m.group(1))}" if m else snake(seg))
        name = "_".join([endpoint.method.lower(), *parts]) or endpoint.method.lower()
    return (name or "call")[:64]


def _unique(name: str, used: set[str]) -> str:
    candidate, i = name, 2
    while candidate in used:
        suffix = f"_{i}"
        candidate = name[: 64 - len(suffix)] + suffix
        i += 1
    used.add(candidate)
    return candidate


def arg_name(api_name: str) -> str:
    name = snake(api_name) or "arg"
    return name if not name[0].isdigit() else f"p_{name}"


def describe(endpoint: Endpoint) -> str:
    head = endpoint.summary.strip() or endpoint.description.strip().split("\n")[0]
    body = endpoint.description.strip()
    text = head
    if body and body != head:
        text = f"{head}\n\n{body}" if head else body
    signature = f"({endpoint.method.upper()} {endpoint.path})"
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    limit = MAX_DESCRIPTION - len(signature) - 2
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return f"{text} {signature}".strip()


def _operation_and_schema(endpoint: Endpoint) -> tuple[Operation, dict[str, Any]]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    bindings: list[ArgBinding] = []

    for p in endpoint.parameters:
        name = arg_name(p.name)
        while name in properties:
            name = f"{p.location.value}_{name}"
        schema = dict(p.json_schema)
        if p.description and "description" not in schema:
            schema["description"] = p.description
        properties[name] = schema
        bindings.append(ArgBinding(arg=name, api_name=p.name, location=p.location.value))
        if p.required:
            required.append(name)

    body_mode: Literal["none", "flat", "wrapped"] = "none"
    content_type = "application/json"
    body = endpoint.request_body
    if body is not None:
        content_type = body.content_type
        schema = body.json_schema or {"type": "object"}
        props = schema.get("properties")
        flat_names = {arg_name(k) for k in props} if isinstance(props, dict) else set()
        can_flatten = (
            schema.get("type") in (None, "object")
            and isinstance(props, dict)
            and props
            and not (flat_names & set(properties))
            and len(flat_names) == len(props)
        )
        if can_flatten:
            body_mode = "flat"
            assert isinstance(props, dict)
            body_required = set(schema.get("required") or [])
            for field_name, field_schema in props.items():
                name = arg_name(field_name)
                properties[name] = field_schema
                bindings.append(ArgBinding(arg=name, api_name=field_name, location="body"))
                if field_name in body_required:
                    required.append(name)
        else:
            body_mode = "wrapped"
            name = "body" if "body" not in properties else "request_body"
            properties[name] = {**schema, "description": body.description or "Request body"}
            bindings.append(ArgBinding(arg=name, api_name="", location="body"))
            if body.required:
                required.append(name)

    page_param = next(
        (b.api_name for b in bindings if b.location == "query" and b.api_name in PAGE_PARAMS),
        None,
    )
    if page_param:
        properties["all_pages"] = {
            "type": "boolean",
            "description": "Fetch consecutive pages (up to 10) and merge the results.",
        }

    success = endpoint.success_response()
    op = Operation(
        method=endpoint.method.lower(),
        path=endpoint.path,
        bindings=bindings,
        body_mode=body_mode,
        content_type=content_type,
        response_schema=success.json_schema if success else None,
        page_param=page_param,
        required_args=sorted(set(required), key=required.index),
        endpoint_key=endpoint.key,
    )
    input_schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = sorted(set(required), key=required.index)
    return op, input_schema


def _resource(path: str) -> str:
    segs = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
    return snake(segs[-1]) if segs else "root"


def _consolidate(endpoints: list[Endpoint], used: set[str]) -> list[ToolSpec]:
    """Merge CRUD endpoints of the same resource into one tool with an `action` arg."""
    groups: dict[str, list[Endpoint]] = defaultdict(list)
    for e in endpoints:
        base = re.sub(r"/{[^}]+}$", "", e.path)
        groups[base].append(e)

    tools: list[ToolSpec] = []
    for base, group in groups.items():
        if len(group) < 2:
            for e in group:
                op, schema = _operation_and_schema(e)
                tools.append(
                    ToolSpec(
                        name=_unique(tool_name(e), used),
                        description=describe(e),
                        input_schema=schema,
                        operations=[op],
                        destructive=e.method.lower() == "delete",
                    )
                )
            continue
        ops: list[Operation] = []
        props: dict[str, Any] = {}
        lines = []
        for e in group:
            op, schema = _operation_and_schema(e)
            op.action = snake(tool_name(e))
            ops.append(op)
            for k, v in schema["properties"].items():
                props.setdefault(k, v)
            req = ", ".join(schema.get("required", [])) or "none"
            lines.append(f"- {op.action}: {describe(e)} Required: {req}.")
        props = {
            "action": {"type": "string", "enum": [o.action for o in ops]},
            **props,
        }
        description = f"Manage {_resource(base)} resources. Actions:\n" + "\n".join(lines)
        tools.append(
            ToolSpec(
                name=_unique(f"manage_{_resource(base)}", used),
                description=description[:MAX_DESCRIPTION],
                input_schema={"type": "object", "properties": props, "required": ["action"]},
                operations=ops,
                destructive=any(o.method == "delete" for o in ops),
            )
        )
    return tools


def design_tools(
    spec: APISpec,
    include_destructive: bool = False,
    include_deprecated: bool = False,
    consolidate: bool = False,
    min_confidence: float = 0.0,
) -> ToolPlan:
    used: set[str] = set()
    skipped: list[str] = []
    kept: list[Endpoint] = []
    for e in spec.endpoints:
        if e.method.lower() == "delete" and not include_destructive:
            skipped.append(f"{e.key}: destructive (use --include-destructive)")
        elif e.deprecated and not include_deprecated:
            skipped.append(f"{e.key}: deprecated")
        elif e.method.lower() in ("head", "options"):
            skipped.append(f"{e.key}: {e.method.upper()} is not useful as a tool")
        elif e.confidence < min_confidence:
            skipped.append(f"{e.key}: low extraction confidence ({e.confidence})")
        else:
            kept.append(e)

    if consolidate:
        return ToolPlan(tools=_consolidate(kept, used), skipped=skipped)

    tools: list[ToolSpec] = []
    for e in kept:
        op, schema = _operation_and_schema(e)
        tools.append(
            ToolSpec(
                name=_unique(tool_name(e), used),
                description=describe(e),
                input_schema=schema,
                operations=[op],
                destructive=e.method.lower() == "delete",
            )
        )
    return ToolPlan(tools=tools, skipped=skipped)


class _Description(BaseModel):
    name: str
    description: str


class _Descriptions(BaseModel):
    tools: list[_Description]


POLISH_SYSTEM = (
    "You write descriptions for MCP tools that wrap HTTP endpoints. Each description must "
    "say what the tool does, when to use it and any important argument constraints, in "
    "at most 3 sentences. Do not invent behaviour that the input does not support."
)


def polish_descriptions(plan: ToolPlan, llm: LLM, batch: int = 20) -> ToolPlan:
    """Rewrite tool descriptions with an LLM so a model can pick the right tool."""
    for i in range(0, len(plan.tools), batch):
        part = plan.tools[i : i + batch]
        listing = "\n\n".join(
            f"name: {t.name}\ncurrent: {t.description}\nargs: {list(t.input_schema['properties'])}"
            for t in part
        )
        result = llm.generate(system=POLISH_SYSTEM, prompt=listing, output=_Descriptions)
        by_name = {d.name: d.description for d in result.tools}
        for t in part:
            new = by_name.get(t.name, "").strip()
            if new:
                sig = " ".join(f"({o.method.upper()} {o.path})" for o in t.operations)
                t.description = f"{new} {sig}"[:MAX_DESCRIPTION]
    return plan
