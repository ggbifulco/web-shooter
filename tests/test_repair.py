import json
from pathlib import Path

from web_shooter.codegen import generate
from web_shooter.ingest.openapi import ingest_openapi
from web_shooter.llm import ScriptedLLM
from web_shooter.pipeline import ShootOptions, shoot
from web_shooter.repair import Edit, RepairLog, apply_edit, build_prompt, repair_loop
from web_shooter.sandbox import SandboxConfig
from web_shooter.testing import run_tests
from web_shooter.tools import design_tools


def _broken_server(petstore_path: str, tmp_path: Path):
    spec = ingest_openapi(petstore_path)
    server = generate(spec, design_tools(spec), tmp_path)
    # Simulate a codegen mistake: the path template no longer matches the binding.
    text = server.config_path.read_text(encoding="utf-8")
    server.config_path.write_text(
        text.replace('"/pets/{petId}"', '"/pets/{pet_id}"'), encoding="utf-8"
    )
    return server


def test_repair_loop_fixes_server(petstore_path: str, tmp_path: Path) -> None:
    server = _broken_server(petstore_path, tmp_path)
    cfg = SandboxConfig(mode="local", mock=True)
    before = run_tests(server, cfg)
    assert not before.ok
    assert any("unfilled path parameter" in r.detail for r in before.failed)

    fix = {
        "analysis": "Path template uses pet_id but the binding fills petId.",
        "edits": [{"file": "tools.json", "old": '"/pets/{pet_id}"', "new": '"/pets/{petId}"'}],
    }
    llm = ScriptedLLM(answers=[fix])
    log_path = tmp_path / "repair_log.json"
    report, log = repair_loop(
        server, lambda: run_tests(server, cfg), llm, log_path=log_path, initial=before
    )
    assert report.ok
    assert log.final_ok and len(log.iterations) == 1
    assert log.iterations[0].edits_applied == 1
    assert log.iterations[0].failures_after == 0
    assert RepairLog.model_validate_json(log_path.read_text(encoding="utf-8")).final_ok
    assert "unfilled path parameter" in llm.prompts[0]  # failure details reach the model


def test_repair_loop_gives_up_after_max_iterations(petstore_path: str, tmp_path: Path) -> None:
    server = _broken_server(petstore_path, tmp_path)
    cfg = SandboxConfig(mode="local", mock=True)
    useless = {"analysis": "?", "edits": [{"file": "tools.json", "old": "not in file", "new": "x"}]}
    llm = ScriptedLLM(answers=[useless, useless])
    report, log = repair_loop(server, lambda: run_tests(server, cfg), llm, max_iterations=2)
    assert not report.ok and not log.final_ok
    assert len(log.iterations) == 2
    assert "found 0 times" in log.iterations[0].edit_errors[0]


def test_apply_edit_rejects_invalid_json(petstore_path: str, tmp_path: Path) -> None:
    spec = ingest_openapi(petstore_path)
    server = generate(spec, design_tools(spec), tmp_path)
    error = apply_edit(server, Edit(file="tools.json", old='"server_name"', new='"server_name'))
    assert error is not None and "invalid JSON" in error
    json.loads(server.config_path.read_text(encoding="utf-8"))  # untouched


def test_build_prompt_contains_files(petstore_path: str, tmp_path: Path) -> None:
    server = _broken_server(petstore_path, tmp_path)
    prompt = build_prompt(server, run_tests(server, SandboxConfig()))
    assert '<file name="tools.json">' in prompt and '<file name="_runtime.py">' in prompt


def test_pipeline_runs_repair_when_llm_available(
    petstore_path: str, tmp_path: Path, monkeypatch
) -> None:
    import web_shooter.pipeline as pipeline

    real_generate = pipeline.generate

    def broken_generate(spec, plan, out_dir, package=None):
        server = real_generate(spec, plan, out_dir, package)
        text = server.config_path.read_text(encoding="utf-8")
        server.config_path.write_text(
            text.replace('"/pets/{petId}"', '"/pets/{pet_id}"'), encoding="utf-8"
        )
        return server

    monkeypatch.setattr(pipeline, "generate", broken_generate)
    fix = {
        "analysis": "fix",
        "edits": [{"file": "tools.json", "old": '"/pets/{pet_id}"', "new": '"/pets/{petId}"'}],
    }
    result = shoot(petstore_path, ShootOptions(out_dir=tmp_path), llm=ScriptedLLM(answers=[fix]))
    assert result.ok
    assert result.repair_log is not None and result.repair_log.final_ok
    assert (result.server.root / "repair_log.json").exists()
