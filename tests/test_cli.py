import json
from pathlib import Path

import pytest

from web_shooter import cli, install
from web_shooter.codegen import generate
from web_shooter.ingest.openapi import ingest_openapi
from web_shooter.tools import design_tools


def test_dry_run(petstore_path: str, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert cli.main(["shoot", petstore_path, "--dry-run", "--no-llm", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Petstore API: 5 endpoints -> 4 tools" in out
    assert "skipped: DELETE /pets/{petId}" in out
    assert not any(tmp_path.iterdir())  # nothing written


def test_shoot_and_test_commands(petstore_path: str, capsys, tmp_path: Path) -> None:
    code = cli.main(["shoot", petstore_path, "--no-llm", "--out", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    summary = json.loads(out.strip().splitlines()[-3])
    assert summary["ok"] is True and summary["tools"] == 4
    assert "petstore-mcp: ready" in out

    assert cli.main(["test", str(tmp_path / "petstore-mcp")]) == 0
    assert "ok   get_pet:valid" in capsys.readouterr().out


def test_bad_source_exit_code(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"not": "a spec"}', encoding="utf-8")
    assert cli.main(["shoot", str(bad), "--no-llm", "--out", str(tmp_path)]) == 2
    assert "not an OpenAPI" in capsys.readouterr().err


def test_install_writes_mcp_json_without_claude_cli(
    petstore_path: str, tmp_path: Path, monkeypatch
) -> None:
    spec = ingest_openapi(petstore_path)
    server = generate(spec, design_tools(spec), tmp_path / "out")
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers": {"other": {"command": "x"}}}', encoding="utf-8"
    )
    msg = install.install(server, scope="project", project_dir=tmp_path)
    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert "other" in data["mcpServers"]  # existing servers are kept
    entry = data["mcpServers"]["petstore-mcp"]
    assert entry["args"] == ["-m", "petstore_mcp"]
    assert Path(entry["env"]["PYTHONPATH"]).is_absolute()
    assert ".mcp.json" in msg


def test_claude_add_command(petstore_path: str, tmp_path: Path) -> None:
    spec = ingest_openapi(petstore_path)
    server = generate(spec, design_tools(spec), tmp_path)
    cmd = install.claude_add_command(server, "user")
    assert cmd[:5] == ["claude", "mcp", "add", "--scope", "user"]
    assert cmd[cmd.index("--") - 1] == "petstore-mcp"
    assert cmd[-2:] == ["-m", "petstore_mcp"]
