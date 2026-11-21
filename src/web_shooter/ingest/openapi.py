"""Ingest OpenAPI 3.x and Swagger 2.0 specs into the IR."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import yaml

from web_shooter.ir import (
    HTTP_METHODS,
    APISpec,
    AuthScheme,
    AuthType,
    Endpoint,
    Parameter,
    ParamLocation,
    RequestBody,
    Response,
)

MAX_REF_DEPTH = 12


class SpecError(ValueError):
    """The document is not a usable OpenAPI/Swagger spec."""


def load_document(source: str, timeout: float = 30.0) -> tuple[dict[str, Any], str]:
    """Load a JSON or YAML document from a URL or a local path.

    Returns the parsed document and the raw text.
    """
    if re.match(r"^https?://", source):
        resp = httpx.get(source, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text
    else:
        text = Path(source).read_text(encoding="utf-8")
    return parse_document(text), text


def parse_document(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise SpecError(f"not valid JSON or YAML: {e}") from e
    if not isinstance(data, dict):
        raise SpecError("spec root must be an object")
    return data


def is_openapi(doc: dict[str, Any]) -> bool:
    return "openapi" in doc or "swagger" in doc


class RefResolver:
    """Inline local `$ref`s (`#/...`). Cycles become a short placeholder object."""

    def __init__(self, root: dict[str, Any]):
        self.root = root

    def lookup(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            raise SpecError(f"external $ref not supported: {ref}")
        node: Any = self.root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise SpecError(f"unresolvable $ref: {ref}")
            node = node[part]
        return node

    def resolve(self, node: Any, seen: tuple[str, ...] = ()) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str):
                ref = node["$ref"]
                name = ref.rsplit("/", 1)[-1]
                if ref in seen or len(seen) >= MAX_REF_DEPTH:
                    return {"type": "object", "description": f"(recursive reference to {name})"}
                try:
                    target = self.lookup(ref)
                except SpecError:
                    return {"description": f"(unresolved reference {ref})"}
                resolved = self.resolve(copy.deepcopy(target), (*seen, ref))
                siblings = {k: v for k, v in node.items() if k != "$ref"}
                if siblings and isinstance(resolved, dict):
                    resolved = {**resolved, **self.resolve(siblings, seen)}
                return resolved
            return {k: self.resolve(v, seen) for k, v in node.items()}
        if isinstance(node, list):
            return [self.resolve(v, seen) for v in node]
        return node


def _clean_schema(schema: Any) -> dict[str, Any]:
    """Drop OpenAPI-only keywords so the result is a plain JSON Schema."""
    if not isinstance(schema, dict):
        return {}
    drop = {"xml", "externalDocs", "example", "discriminator", "readOnly", "writeOnly"}
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in drop or k.startswith("x-"):
            continue
        if k == "nullable":
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _clean_schema(pv) for pk, pv in v.items()}
        elif k in ("items", "additionalProperties", "not") and isinstance(v, dict):
            out[k] = _clean_schema(v)
        elif k in ("allOf", "anyOf", "oneOf") and isinstance(v, list):
            out[k] = [_clean_schema(s) for s in v]
        else:
            out[k] = v
    if out.get("type") == "file":  # Swagger 2.0 only; not a JSON Schema type
        out["type"] = "string"
        out["format"] = "binary"
    if schema.get("nullable") is True and isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
    return out


def _auth_schemes(defs: dict[str, Any]) -> list[AuthScheme]:
    schemes: list[AuthScheme] = []
    for name, d in (defs or {}).items():
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        desc = d.get("description", "")
        if t == "apiKey":
            loc = d.get("in", "header")
            schemes.append(
                AuthScheme(
                    name=name,
                    type=AuthType.API_KEY,
                    param_name=d.get("name", "X-API-Key"),
                    location=ParamLocation(loc)
                    if loc in ParamLocation._value2member_map_
                    else None,
                    description=desc,
                )
            )
        elif t == "http" and str(d.get("scheme", "")).lower() == "basic" or t == "basic":
            schemes.append(AuthScheme(name=name, type=AuthType.BASIC, description=desc))
        elif t == "http":
            schemes.append(AuthScheme(name=name, type=AuthType.BEARER, description=desc))
        elif t in ("oauth2", "openIdConnect"):
            schemes.append(AuthScheme(name=name, type=AuthType.OAUTH2, description=desc))
    return schemes


def _security_names(security: Any) -> list[str]:
    names: list[str] = []
    for req in security or []:
        if isinstance(req, dict):
            names.extend(n for n in req if n not in names)
    return names


def _merge_params(path_level: list[Any], op_level: list[Any]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for p in [*path_level, *op_level]:
        if isinstance(p, dict) and "name" in p:
            merged[(p["name"], p.get("in", "query"))] = p
    return list(merged.values())


def _param(p: dict[str, Any], v2: bool) -> Parameter | None:
    loc = p.get("in", "query")
    if loc not in ParamLocation._value2member_map_:
        return None
    if v2:
        schema = {k: v for k, v in p.items() if k in ("type", "format", "enum", "items", "default")}
    else:
        schema = p.get("schema") or {"type": "string"}
    return Parameter(
        name=p["name"],
        location=ParamLocation(loc),
        required=bool(p.get("required", loc == "path")),
        description=p.get("description", ""),
        json_schema=_clean_schema(schema) or {"type": "string"},
    )


def _pick_content(content: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    if not content:
        return None
    for ct in content:
        if "json" in ct:
            return ct, content[ct] or {}
    ct = next(iter(content))
    return ct, content[ct] or {}


def _base_url_v3(doc: dict[str, Any], source: str) -> str:
    servers = doc.get("servers") or []
    url = ""
    if servers and isinstance(servers[0], dict):
        url = servers[0].get("url", "")
        for var, spec in (servers[0].get("variables") or {}).items():
            url = url.replace("{" + var + "}", str(spec.get("default", "")))
    if url and not re.match(r"^https?://", url) and re.match(r"^https?://", source):
        url = urljoin(source, url)
    return url.rstrip("/")


def _base_url_v2(doc: dict[str, Any], source: str) -> str:
    host = doc.get("host")
    base_path = doc.get("basePath", "")
    if not host:
        if re.match(r"^https?://", source):
            return urljoin(source, base_path or "/").rstrip("/")
        return base_path.rstrip("/")
    schemes = doc.get("schemes") or ["https"]
    scheme = "https" if "https" in schemes else schemes[0]
    return f"{scheme}://{host}{base_path}".rstrip("/")


def from_openapi(doc: dict[str, Any], source: str = "") -> APISpec:
    """Map a parsed OpenAPI 3.x / Swagger 2.0 document to the IR."""
    if not is_openapi(doc):
        raise SpecError("document has no 'openapi' or 'swagger' field")
    v2 = "swagger" in doc
    resolver = RefResolver(doc)
    info = doc.get("info") or {}

    if v2:
        base_url = _base_url_v2(doc, source)
        schemes = _auth_schemes(doc.get("securityDefinitions") or {})
        global_consumes = doc.get("consumes") or ["application/json"]
    else:
        base_url = _base_url_v3(doc, source)
        components = doc.get("components") or {}
        schemes = _auth_schemes(resolver.resolve(components.get("securitySchemes") or {}))
        global_consumes = []
    global_security = _security_names(doc.get("security"))

    endpoints: list[Endpoint] = []
    for path, item in (doc.get("paths") or {}).items():
        item = resolver.resolve(item) if isinstance(item, dict) else {}
        path_params = item.get("parameters") or []
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            raw_params = _merge_params(path_params, op.get("parameters") or [])
            params: list[Parameter] = []
            body: RequestBody | None = None

            if v2:
                form_props: dict[str, Any] = {}
                form_required: list[str] = []
                for p in raw_params:
                    loc = p.get("in")
                    if loc == "body":
                        body = RequestBody(
                            content_type=(op.get("consumes") or global_consumes)[0],
                            required=bool(p.get("required")),
                            description=p.get("description", ""),
                            json_schema=_clean_schema(p.get("schema") or {}),
                        )
                    elif loc == "formData":
                        form_props[p["name"]] = _clean_schema(
                            {k: v for k, v in p.items() if k in ("type", "format", "enum", "items")}
                        )
                        if p.get("description"):
                            form_props[p["name"]]["description"] = p["description"]
                        if p.get("required"):
                            form_required.append(p["name"])
                    elif (param := _param(p, v2=True)) is not None:
                        params.append(param)
                if form_props and body is None:
                    consumes = op.get("consumes") or global_consumes
                    body = RequestBody(
                        content_type=next(
                            (c for c in consumes if "form" in c),
                            "application/x-www-form-urlencoded",
                        ),
                        required=bool(form_required),
                        json_schema={
                            "type": "object",
                            "properties": form_props,
                            "required": form_required,
                        },
                    )
            else:
                for p in raw_params:
                    if (param := _param(p, v2=False)) is not None:
                        params.append(param)
                rb = op.get("requestBody")
                if isinstance(rb, dict):
                    picked = _pick_content(rb.get("content") or {})
                    if picked:
                        ct, media = picked
                        body = RequestBody(
                            content_type=ct,
                            required=bool(rb.get("required")),
                            description=rb.get("description", ""),
                            json_schema=_clean_schema(media.get("schema") or {}),
                        )

            responses: list[Response] = []
            for status, r in (op.get("responses") or {}).items():
                if not isinstance(r, dict):
                    continue
                schema: dict[str, Any] | None = None
                if v2:
                    if r.get("schema"):
                        schema = _clean_schema(r["schema"])
                else:
                    picked = _pick_content(r.get("content") or {})
                    if picked and picked[1].get("schema"):
                        schema = _clean_schema(picked[1]["schema"])
                responses.append(
                    Response(
                        status=str(status), description=r.get("description", ""), json_schema=schema
                    )
                )

            security = op.get("security")
            endpoints.append(
                Endpoint(
                    method=method,
                    path=path,
                    operation_id=op.get("operationId"),
                    summary=op.get("summary", "") or "",
                    description=op.get("description", "") or "",
                    tags=list(op.get("tags") or []),
                    parameters=params,
                    request_body=body,
                    responses=responses,
                    auth=_security_names(security) if security is not None else global_security,
                    deprecated=bool(op.get("deprecated")),
                )
            )

    return APISpec(
        title=info.get("title") or "API",
        version=str(info.get("version", "")),
        description=info.get("description", "") or "",
        base_url=base_url,
        auth_schemes=schemes,
        endpoints=endpoints,
        origin=source,
    )


def ingest_openapi(source: str) -> APISpec:
    doc, _ = load_document(source)
    return from_openapi(doc, source)
