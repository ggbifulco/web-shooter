"""Normalized intermediate representation (IR) of an HTTP API.

Every ingester (OpenAPI, HTML docs) produces an `APISpec`; the tool designer and the
code generator only ever consume this model.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


class ParamLocation(StrEnum):
    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"


class AuthType(StrEnum):
    API_KEY = "api_key"
    BEARER = "bearer"
    BASIC = "basic"
    OAUTH2 = "oauth2"


class Parameter(BaseModel):
    name: str
    location: ParamLocation
    required: bool = False
    description: str = ""
    json_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "string"})


class RequestBody(BaseModel):
    content_type: str = "application/json"
    required: bool = False
    description: str = ""
    json_schema: dict[str, Any] = Field(default_factory=dict)


class Response(BaseModel):
    status: str
    description: str = ""
    json_schema: dict[str, Any] | None = None


class AuthScheme(BaseModel):
    name: str
    type: AuthType
    # For api_key: the header/query/cookie parameter that carries the key.
    param_name: str | None = None
    location: ParamLocation | None = None
    description: str = ""

    @property
    def env_var(self) -> str:
        return "API_" + "".join(c if c.isalnum() else "_" for c in self.name).upper()


class Source(BaseModel):
    """Where a piece of information came from (used by the HTML ingester)."""

    url: str
    excerpt: str = ""


class Endpoint(BaseModel):
    method: str
    path: str
    operation_id: str | None = None
    summary: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    parameters: list[Parameter] = Field(default_factory=list)
    request_body: RequestBody | None = None
    responses: list[Response] = Field(default_factory=list)
    auth: list[str] = Field(default_factory=list)
    deprecated: bool = False
    sources: list[Source] = Field(default_factory=list)
    confidence: float = 1.0
    missing: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.method.upper()} {self.path}"

    def success_response(self) -> Response | None:
        for r in sorted(self.responses, key=lambda r: r.status):
            if r.status.startswith("2"):
                return r
        return None


class APISpec(BaseModel):
    title: str
    version: str = ""
    description: str = ""
    base_url: str = ""
    auth_schemes: list[AuthScheme] = Field(default_factory=list)
    endpoints: list[Endpoint] = Field(default_factory=list)
    origin: str = ""

    def to_json(self) -> str:
        return self.model_dump_json(indent=2, exclude_none=True)

    @classmethod
    def from_json(cls, data: str) -> APISpec:
        return cls.model_validate(json.loads(data))

    def save(self, path: Path) -> None:
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> APISpec:
        return cls.from_json(path.read_text(encoding="utf-8"))
