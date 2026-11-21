from pathlib import Path

import pytest

from web_shooter.pipeline import ShootOptions, shoot
from web_shooter.sandbox import SandboxConfig, docker_available, server_params

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="Docker daemon not running"),
]


def test_shoot_in_docker_sandbox(petstore_path: str, tmp_path: Path) -> None:
    cfg = SandboxConfig(mode="docker", mock=True, suite_timeout=600)
    result = shoot(petstore_path, ShootOptions(out_dir=tmp_path, sandbox=cfg))
    assert result.ok, result.report.summary()
    assert result.report.passed == 9  # 4 tools without DELETE


def test_docker_run_is_locked_down(petstore_path: str, tmp_path: Path) -> None:
    result = shoot(petstore_path, ShootOptions(out_dir=tmp_path))
    params = server_params(result.server, SandboxConfig(mode="docker", mock=True))
    args = params.args
    assert args[args.index("--network") + 1] == "none"
    for flag in ("--read-only", "--memory", "--pids-limit", "--cap-drop"):
        assert flag in args
