"""Full pipeline, the way a user runs it."""

import json
from pathlib import Path

import anyio
from mcp import Client

from web_shooter.pipeline import ShootOptions, shoot
from web_shooter.sandbox import SandboxConfig, server_params


def test_shoot_mock(petstore_path: str, tmp_path: Path) -> None:
    lines: list[str] = []
    result = shoot(petstore_path, ShootOptions(out_dir=tmp_path), progress=lines.append)
    assert result.ok
    assert [t.name for t in result.plan.tools] == [
        "list_pets",
        "create_pet",
        "get_pet",
        "get_health",
    ]
    report = json.loads((result.server.root / "test_report.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert any(line.startswith("[5/5] repair   skipped (all tests pass)") for line in lines)


def test_generated_server_real_calls_with_pagination(
    petstore_path: str, tmp_path: Path, fake_api: str
) -> None:
    env = {"PETSTORE_BASE_URL": f"{fake_api}/v1", "PETSTORE_API_KEY": "secret"}
    cfg = SandboxConfig(mode="local", mock=False, env=env)
    result = shoot(petstore_path, ShootOptions(out_dir=tmp_path, sandbox=cfg))
    assert result.ok

    async def scenario() -> dict[str, object]:
        out: dict[str, object] = {}
        async with Client(server_params(result.server, cfg), read_timeout_seconds=30) as client:
            first = await client.call_tool("list_pets", {})
            out["first_page"] = json.loads(first.content[0].text)
            every = await client.call_tool("list_pets", {"all_pages": True})
            out["all"] = json.loads(every.content[0].text)
            created = await client.call_tool("create_pet", {"name": "Nemo", "tag": "fish"})
            out["created"] = json.loads(created.content[0].text)
            missing = await client.call_tool("get_pet", {"pet_id": 999})
            out["missing"] = (missing.is_error, missing.content[0].text)
        return out

    out = anyio.run(scenario)
    assert len(out["first_page"]["data"]) == 2
    # 5 seeded pets + 1 created by the generated `create_pet:valid` test,
    # 2 per page -> 3 pages merged
    assert [p["id"] for p in out["all"]] == [1, 2, 3, 4, 5, 6]
    assert out["created"]["name"] == "Nemo"
    assert out["missing"][0] is True and out["missing"][1].startswith("HTTP 404")
