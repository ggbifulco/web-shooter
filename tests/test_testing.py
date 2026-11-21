from pathlib import Path

from web_shooter.codegen import generate
from web_shooter.ingest.openapi import ingest_openapi
from web_shooter.sandbox import SandboxConfig
from web_shooter.testing import generate_tests, run_tests, validate_response
from web_shooter.tools import design_tools


def _server(path: str, tmp_path: Path, **kw):
    spec = ingest_openapi(path)
    return generate(spec, design_tools(spec, **kw), tmp_path)


def test_generate_tests_kinds(petstore_path: str, tmp_path: Path) -> None:
    cases = generate_tests(_server(petstore_path, tmp_path).config())
    by_id = {c.id: c for c in cases}
    assert by_id["get_pet:valid"].arguments == {"pet_id": 1}
    assert by_id["get_pet:missing_required"].arguments == {}
    assert by_id["get_pet:wrong_type"].arguments == {"pet_id": "not-a-number"}
    assert by_id["create_pet:valid"].arguments == {"name": "example"}
    assert all(c.expect_error for c in cases if c.kind != "valid")


def test_generate_tests_consolidated(swagger_path: str, tmp_path: Path) -> None:
    cases = generate_tests(_server(swagger_path, tmp_path, consolidate=True).config())
    ids = {c.id for c in cases}
    assert {"manage_notes[list_notes]:valid", "manage_notes[create_note]:valid"} <= ids
    assert "manage_notes[create_note]:missing_required" in ids


def test_validate_response() -> None:
    schema = {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}
    assert validate_response('{"id": 1}', schema) is None
    assert "does not match" in validate_response('{"id": "x"}', schema)
    assert validate_response("not json", schema) == "response is not JSON"
    assert validate_response("anything", None) is None
    # patterns are ignored: test data is not expected to satisfy them
    assert validate_response('"abc"', {"type": "string", "pattern": "^[0-9]+$"}) is None


def test_mock_suite_passes(petstore_path: str, tmp_path: Path) -> None:
    server = _server(petstore_path, tmp_path, include_destructive=True)
    report = run_tests(server, SandboxConfig(mode="local", mock=True))
    assert report.ok, report.model_dump()
    assert report.passed == 12


def test_live_suite_against_fake_api(petstore_path: str, tmp_path: Path, fake_api: str) -> None:
    server = _server(petstore_path, tmp_path, include_destructive=True)
    env = {"PETSTORE_BASE_URL": f"{fake_api}/v1", "PETSTORE_API_KEY": "secret"}
    report = run_tests(server, SandboxConfig(mode="local", mock=False, env=env))
    assert report.ok, [r.model_dump() for r in report.failed]
    assert report.passed == 12


def test_live_suite_wrong_key_is_inconclusive_not_passed(
    petstore_path: str, tmp_path: Path, fake_api: str
) -> None:
    server = _server(petstore_path, tmp_path)
    env = {"PETSTORE_BASE_URL": f"{fake_api}/v1", "PETSTORE_API_KEY": "wrong"}
    report = run_tests(server, SandboxConfig(mode="local", mock=False, env=env))
    statuses = {r.id: r.status for r in report.results}
    assert statuses["get_pet:valid"] == "inconclusive"
    assert statuses["get_health:valid"] == "passed"  # no auth needed


def test_server_that_cannot_start_is_reported(petstore_path: str, tmp_path: Path) -> None:
    server = _server(petstore_path, tmp_path)
    (server.package_dir / "_runtime.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    report = run_tests(server, SandboxConfig(mode="local", mock=True, suite_timeout=60))
    assert not report.ok
    assert not report.listing_ok
