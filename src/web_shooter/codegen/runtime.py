"""Runtime shipped inside every generated MCP server (copied as `_runtime.py`).

It must only depend on: mcp, httpx, jsonschema, anyio. It reads `tools.json` (written
by the generator) and turns each MCP tool call into an HTTP request.

Environment variables (PREFIX is set per generated server):
  PREFIX_BASE_URL       override the API base URL
  PREFIX_<SCHEME>       credential for each auth scheme listed in tools.json
  WEB_SHOOTER_MOCK=1    do not call the API; answer with data shaped like the response schema
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import anyio
import httpx
import jsonschema
import mcp_types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

MAX_PAGES = 10
MAX_RESULT_CHARS = 20_000
RETRY_STATUSES = {429, 502, 503, 504}
LIST_KEYS = ("data", "items", "results", "records", "entries", "values")


def sample_from_schema(schema: Any, depth: int = 0, required_only: bool = False) -> Any:
    """Build a small value that validates against a (reasonable) JSON Schema."""
    if not isinstance(schema, dict) or depth > 6:
        return None
    if "const" in schema:
        return schema["const"]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "example" in schema:
        return schema["example"]
    for key in ("oneOf", "anyOf"):
        if schema.get(key):
            return sample_from_schema(schema[key][0], depth + 1, required_only)
    if schema.get("allOf"):
        merged: dict[str, Any] = {}
        for part in schema["allOf"]:
            value = sample_from_schema(part, depth + 1, required_only)
            if isinstance(value, dict):
                merged.update(value)
        return merged
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if t is None:
        t = "object" if "properties" in schema else "string"
    if t == "object":
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        return {
            k: sample_from_schema(v, depth + 1, required_only)
            for k, v in props.items()
            if not required_only or k in required
        }
    if t == "array":
        item = sample_from_schema(
            schema.get("items") or {"type": "string"}, depth + 1, required_only
        )
        n = max(1, int(schema.get("minItems", 1)))
        return [item] * n
    if t == "integer":
        lo = schema.get("minimum", 1)
        return int(lo) if isinstance(lo, (int, float)) else 1
    if t == "number":
        lo = schema.get("minimum", 1.5)
        return float(lo) if isinstance(lo, (int, float)) else 1.5
    if t == "boolean":
        return True
    if t == "null":
        return None
    fmt = schema.get("format", "")
    samples = {
        "date-time": "2024-01-01T00:00:00Z",
        "date": "2024-01-01",
        "email": "user@example.com",
        "uri": "https://example.com",
        "url": "https://example.com",
        "uuid": "00000000-0000-4000-8000-000000000000",
    }
    value = samples.get(fmt, "example")
    min_len = int(schema.get("minLength", 0) or 0)
    if len(value) < min_len:
        value = value + "x" * (min_len - len(value))
    return value


def extract_items(data: Any) -> list[Any] | None:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in LIST_KEYS:
            if isinstance(data.get(key), list):
                return data[key]
    return None


class ToolError(Exception):
    pass


class ApiRuntime:
    def __init__(self, config: dict[str, Any], env: dict[str, str] | None = None):
        self.env = dict(os.environ if env is None else env)
        self.prefix = config["env_prefix"]
        self.base_url = (self.env.get(f"{self.prefix}_BASE_URL") or config["base_url"]).rstrip("/")
        self.auth = config.get("auth", [])
        self.tools = {t["name"]: t for t in config["tools"]}
        self.mock = self.env.get("WEB_SHOOTER_MOCK") == "1"

    # ------------------------------------------------------------ request building

    def _operation(self, tool: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        ops = tool["operations"]
        if len(ops) == 1:
            return ops[0]
        action = args.get("action")
        for op in ops:
            if op["action"] == action:
                return op
        raise ToolError(f"unknown action {action!r}; expected one of {[o['action'] for o in ops]}")

    def _credentials(self) -> tuple[dict[str, str], dict[str, str], dict[str, str], Any]:
        headers: dict[str, str] = {}
        query: dict[str, str] = {}
        cookies: dict[str, str] = {}
        basic = None
        for scheme in self.auth:
            value = self.env.get(scheme["env_var"])
            if not value:
                continue
            kind = scheme["type"]
            if kind == "api_key":
                where = scheme.get("location") or "header"
                name = scheme.get("param_name") or "X-API-Key"
                {"header": headers, "query": query, "cookie": cookies}.get(where, headers)[name] = (
                    value
                )
            elif kind in ("bearer", "oauth2"):
                headers["Authorization"] = f"Bearer {value}"
            elif kind == "basic":
                user, _, password = value.partition(":")
                basic = (user, password)
        return headers, query, cookies, basic

    def build_request(self, tool: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        op = self._operation(tool, args)
        missing = [a for a in op.get("required_args", []) if args.get(a) is None]
        if missing:
            raise ToolError(f"missing required argument(s) for this action: {missing}")
        path = op["path"]
        query: dict[str, Any] = {}
        headers: dict[str, str] = {}
        cookies: dict[str, str] = {}
        body: Any = None
        flat_body: dict[str, Any] = {}
        for b in op["bindings"]:
            if b["arg"] not in args or args[b["arg"]] is None:
                continue
            value = args[b["arg"]]
            loc = b["location"]
            if loc == "path":
                path = path.replace("{" + b["api_name"] + "}", quote(str(value), safe=""))
            elif loc == "query":
                if isinstance(value, bool):
                    value = str(value).lower()
                query[b["api_name"]] = value
            elif loc == "header":
                headers[b["api_name"]] = str(value)
            elif loc == "cookie":
                cookies[b["api_name"]] = str(value)
            elif loc == "body":
                if op["body_mode"] == "wrapped":
                    body = value
                else:
                    flat_body[b["api_name"]] = value
        if op["body_mode"] == "flat" and flat_body:
            body = flat_body
        if "{" in path:
            raise ToolError(f"unfilled path parameter in {path}")

        auth_headers, auth_query, auth_cookies, basic = self._credentials()
        url = self.base_url + path
        base_host = urlparse(self.base_url).netloc
        if urlparse(url).netloc != base_host:
            raise ToolError(f"refusing request to {url}: host is not {base_host}")
        request: dict[str, Any] = {
            "method": op["method"].upper(),
            "url": url,
            "params": {**query, **auth_query},
            "headers": {**headers, **auth_headers},
            "cookies": {**cookies, **auth_cookies},
            "auth": basic,
        }
        if body is not None:
            if "form" in op.get("content_type", ""):
                request["data"] = body
            else:
                request["json"] = body
        return request

    # ------------------------------------------------------------ execution

    async def _send(self, client: httpx.AsyncClient, request: dict[str, Any]) -> httpx.Response:
        req = {k: v for k, v in request.items() if v not in (None, {})}
        for attempt in range(3):
            resp = await client.request(**req)
            if resp.status_code not in RETRY_STATUSES or attempt == 2:
                return resp
            await anyio.sleep(0.5 * 2**attempt)
        return resp

    @staticmethod
    def _decode(resp: httpx.Response) -> Any:
        if not resp.content:
            return {"status": resp.status_code}
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool {name!r}")
        try:
            jsonschema.validate(args, tool["input_schema"])
        except jsonschema.ValidationError as e:
            raise ToolError(f"invalid arguments: {e.message}") from e
        except jsonschema.SchemaError:
            pass  # a broken schema should not make the tool unusable
        op = self._operation(tool, args)
        request = self.build_request(tool, args)

        if self.mock:
            schema = op.get("response_schema") or {"type": "object"}
            random.seed(name)
            return sample_from_schema(schema)

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await self._send(client, request)
            if resp.status_code >= 400:
                raise ToolError(f"HTTP {resp.status_code}: {resp.text[:2000]}")
            data = self._decode(resp)
            page_param = op.get("page_param")
            if not (args.get("all_pages") and page_param):
                return data
            items = extract_items(data)
            if items is None:
                return data
            merged = list(items)
            page = args.get(next(b["arg"] for b in op["bindings"] if b["api_name"] == page_param))
            offset_mode = page_param == "offset"
            current = int(page or (0 if offset_mode else 1))
            for _ in range(MAX_PAGES - 1):
                current = current + len(items) if offset_mode else current + 1
                request["params"] = {**request["params"], page_param: current}
                resp = await self._send(client, request)
                if resp.status_code >= 400:
                    break
                items = extract_items(self._decode(resp)) or []
                if not items:
                    break
                merged.extend(items)
            return merged


def format_result(data: Any) -> str:
    text = data if isinstance(data, str) else json.dumps(data, indent=2, ensure_ascii=False)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n... (truncated, {len(text)} chars total)"
    return text


def build_server(config: dict[str, Any], env: dict[str, str] | None = None) -> Server[Any]:
    runtime = ApiRuntime(config, env)

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=t["name"], description=t["description"], input_schema=t["input_schema"]
                )
                for t in config["tools"]
            ]
        )

    async def call_tool(ctx: Any, params: Any) -> types.CallToolResult:
        try:
            data = await runtime.call(params.name, dict(params.arguments or {}))
        except ToolError as e:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(e))], is_error=True
            )
        except httpx.HTTPError as e:
            msg = f"request failed: {type(e).__name__}: {e}"
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=msg)], is_error=True
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=format_result(data))], is_error=False
        )

    return Server(config["server_name"], on_list_tools=list_tools, on_call_tool=call_tool)


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def serve(config_path: Path) -> None:
    server = build_server(load_config(config_path))

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)
