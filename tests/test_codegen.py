import json
from pathlib import Path

import anyio
import jsonschema
import pytest

from web_shooter.codegen import generate
from web_shooter.codegen.generator import names_for
from web_shooter.codegen.runtime import ApiRuntime, ToolError, extract_items, sample_from_schema
from web_shooter.ingest.openapi import ingest_openapi
from web_shooter.tools import design_tools


@pytest.fixture
def generated(petstore_path: str, tmp_path: Path):
    spec = ingest_openapi(petstore_path)
    return generate(spec, design_tools(spec, include_destructive=True), tmp_path)


def test_names_for() -> None:
    assert names_for("Petstore API") == ("petstore_mcp", "petstore-mcp", "PETSTORE")
    assert names_for("123 Weird!! REST")[0] == "api_123_weird_mcp"


def test_generated_package_layout(generated) -> None:
    for rel in ["pyproject.toml", "README.md", "Dockerfile", "api_spec.json"]:
        assert (generated.root / rel).exists(), rel
    for rel in ["server.py", "__main__.py", "_runtime.py", "tools.json", "__init__.py"]:
        assert (generated.package_dir / rel).exists(), rel
    config = generated.config()
    assert config["env_prefix"] == "PETSTORE"
    assert config["auth"][0]["env_var"] == "PETSTORE_API_KEY"
    readme = (generated.root / "README.md").read_text(encoding="utf-8")
    assert "`get_pet`" in readme and "PETSTORE_API_KEY" in readme
    pyproject = (generated.root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'petstore-mcp = "petstore_mcp.server:main"' in pyproject


def test_generated_server_module_compiles(generated) -> None:
    for py in generated.package_dir.glob("*.py"):
        compile(py.read_text(encoding="utf-8"), str(py), "exec")


def _runtime(generated, **env: str) -> ApiRuntime:
    return ApiRuntime(generated.config(), env={"PETSTORE_BASE_URL": "https://api.test/v1", **env})


def test_build_request_path_query_auth(generated) -> None:
    rt = _runtime(generated, PETSTORE_API_KEY="k")
    req = rt.build_request(rt.tools["get_pet"], {"pet_id": "a/b"})
    assert req["url"] == "https://api.test/v1/pets/a%2Fb"
    assert req["headers"] == {"X-API-Key": "k"}
    req = rt.build_request(rt.tools["list_pets"], {"page": 2, "tag": "dog"})
    assert req["params"] == {"page": 2, "tag": "dog"}


def test_build_request_flat_body(generated) -> None:
    rt = _runtime(generated)
    req = rt.build_request(rt.tools["create_pet"], {"name": "Rex"})
    assert (req["method"], req["json"]) == ("POST", {"name": "Rex"})


def test_build_request_missing_required(generated) -> None:
    rt = _runtime(generated)
    with pytest.raises(ToolError, match="missing"):
        rt.build_request(rt.tools["get_pet"], {})


def test_host_allowlist(generated) -> None:
    config = generated.config()
    config["tools"][0]["operations"][0]["path"] = "@evil.test/x"
    rt = ApiRuntime(config, env={"PETSTORE_BASE_URL": "https://api.test"})
    with pytest.raises(ToolError, match="refusing"):
        rt.build_request(rt.tools[config["tools"][0]["name"]], {})


def test_query_and_basic_auth(swagger_path: str, tmp_path: Path) -> None:
    spec = ingest_openapi(swagger_path)
    server = generate(spec, design_tools(spec), tmp_path)
    rt = ApiRuntime(server.config(), env={"NOTES_TOKEN": "t0k", "NOTES_BASIC": "me:pw"})
    req = rt.build_request(rt.tools["upload_attachment"], {"id": 3, "filename": "a.txt"})
    assert req["params"] == {"token": "t0k"}
    assert req["auth"] == ("me", "pw")
    assert req["data"] == {"filename": "a.txt"}  # form body, not JSON


def test_mock_call_returns_schema_shaped_data(generated) -> None:
    rt = _runtime(generated, WEB_SHOOTER_MOCK="1")
    data = anyio.run(rt.call, "get_pet", {"pet_id": 1})
    schema = rt.tools["get_pet"]["operations"][0]["response_schema"]
    jsonschema.validate(data, schema)


def test_invalid_arguments_rejected(generated) -> None:
    rt = _runtime(generated, WEB_SHOOTER_MOCK="1")
    with pytest.raises(ToolError, match="invalid arguments"):
        anyio.run(rt.call, "get_pet", {"pet_id": "not-int"})
    with pytest.raises(ToolError, match="unknown tool"):
        anyio.run(rt.call, "nope", {})


@pytest.mark.parametrize(
    "schema",
    [
        {
            "type": "object",
            "required": ["a"],
            "properties": {"a": {"type": "string", "minLength": 10}},
        },
        {"type": "array", "items": {"type": "integer", "minimum": 5}, "minItems": 2},
        {"oneOf": [{"type": "boolean"}, {"type": "string"}]},
        {"type": ["string", "null"], "format": "date-time"},
        {"enum": ["x", "y"]},
        {
            "allOf": [
                {"type": "object", "properties": {"a": {"type": "integer"}}},
                {"properties": {"b": {"type": "number"}}},
            ]
        },
    ],
)
def test_sample_from_schema_is_valid(schema: dict) -> None:
    jsonschema.validate(sample_from_schema(schema), schema)


def test_extract_items() -> None:
    assert extract_items([1]) == [1]
    assert extract_items({"results": [2]}) == [2]
    assert extract_items({"x": 1}) is None


def test_tools_json_is_plain_json(generated) -> None:
    json.loads(generated.config_path.read_text(encoding="utf-8"))
