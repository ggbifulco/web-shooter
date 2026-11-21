import pytest

from web_shooter.ingest import SpecError, ingest
from web_shooter.ingest.openapi import RefResolver, from_openapi, ingest_openapi, parse_document
from web_shooter.ir import APISpec, AuthType, ParamLocation


def _ep(spec: APISpec, key: str):
    return next(e for e in spec.endpoints if e.key == key)


def test_openapi3_endpoints_and_base_url(petstore_path: str) -> None:
    spec = ingest_openapi(petstore_path)
    assert spec.title == "Petstore API"
    assert spec.base_url == "http://localhost:8765/v1"  # server variable default applied
    assert {e.key for e in spec.endpoints} == {
        "GET /pets",
        "POST /pets",
        "GET /pets/{petId}",
        "DELETE /pets/{petId}",
        "GET /health",
    }


def test_openapi3_path_level_params_and_refs(petstore_path: str) -> None:
    spec = ingest_openapi(petstore_path)
    get_pet = _ep(spec, "GET /pets/{petId}")
    assert [(p.name, p.location, p.required) for p in get_pet.parameters] == [
        ("petId", ParamLocation.PATH, True)
    ]
    schema = get_pet.success_response().json_schema
    # allOf + nested $ref resolved, recursion (Pet -> Owner -> Pet) cut with a placeholder
    owner = schema["allOf"][1]["properties"]["owner"]
    assert owner["properties"]["pets"]["items"]["description"].startswith("(recursive")


def test_openapi3_body_nullable_and_security(petstore_path: str) -> None:
    spec = ingest_openapi(petstore_path)
    create = _ep(spec, "POST /pets")
    assert create.request_body is not None and create.request_body.required
    assert create.request_body.json_schema["properties"]["tag"]["type"] == ["string", "null"]
    assert create.auth == ["apiKey"]
    assert _ep(spec, "GET /health").auth == []  # explicit `security: []` overrides global
    [scheme] = spec.auth_schemes
    assert scheme.type == AuthType.API_KEY
    assert (scheme.param_name, scheme.location) == ("X-API-Key", ParamLocation.HEADER)


def test_swagger2_conversion(swagger_path: str) -> None:
    spec = ingest_openapi(swagger_path)
    assert spec.base_url == "https://notes.example.com/api"
    create = _ep(spec, "POST /notes")
    assert create.request_body.json_schema["required"] == ["text"]
    upload = _ep(spec, "POST /notes/{id}/attachments")
    assert upload.request_body.content_type == "application/x-www-form-urlencoded"
    assert upload.request_body.json_schema["required"] == ["filename"]
    assert upload.auth == ["basic"]
    types = {s.name: s.type for s in spec.auth_schemes}
    assert types == {"token": AuthType.API_KEY, "basic": AuthType.BASIC}
    token = next(s for s in spec.auth_schemes if s.name == "token")
    assert token.location == ParamLocation.QUERY
    offset = _ep(spec, "GET /notes").parameters[0]
    assert offset.json_schema == {"type": "integer"}


def test_load_from_url(fake_api: str) -> None:
    spec = ingest(f"{fake_api}/openapi.yaml")
    assert len(spec.endpoints) == 5


def test_relative_server_url_resolved_against_source() -> None:
    doc = {"openapi": "3.1.0", "info": {"title": "T"}, "servers": [{"url": "/api"}], "paths": {}}
    assert from_openapi(doc, "https://x.dev/spec.json").base_url == "https://x.dev/api"


def test_not_a_spec() -> None:
    with pytest.raises(SpecError):
        from_openapi({"hello": "world"})
    with pytest.raises(SpecError):
        parse_document("- just\n- a list\n")


def test_ref_resolver_bad_ref_is_tolerated() -> None:
    out = RefResolver({}).resolve({"$ref": "#/components/schemas/Missing"})
    assert "unresolved" in out["description"]


def test_ir_roundtrip(petstore_path: str, tmp_path) -> None:
    spec = ingest_openapi(petstore_path)
    spec.save(tmp_path / "spec.json")
    assert APISpec.load(tmp_path / "spec.json") == spec
