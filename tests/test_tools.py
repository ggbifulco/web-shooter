from web_shooter.ingest.openapi import ingest_openapi
from web_shooter.ir import APISpec, Endpoint, Parameter, ParamLocation, RequestBody
from web_shooter.llm import ScriptedLLM
from web_shooter.tools import TOOL_NAME_RE, design_tools, polish_descriptions, tool_name


def test_tool_names() -> None:
    assert tool_name(Endpoint(method="get", path="/x", operation_id="listPets")) == "list_pets"
    assert (
        tool_name(Endpoint(method="get", path="/users/{userId}/repos"))
        == "get_users_by_user_id_repos"
    )
    long = tool_name(Endpoint(method="get", path="/" + "/".join(["segment"] * 20)))
    assert TOOL_NAME_RE.match(long)


def test_names_are_unique() -> None:
    spec = APISpec(
        title="t",
        endpoints=[
            Endpoint(method="get", path="/a", operation_id="same"),
            Endpoint(method="post", path="/a", operation_id="same"),
        ],
    )
    names = [t.name for t in design_tools(spec).tools]
    assert names == ["same", "same_2"]


def test_destructive_and_deprecated_are_skipped(petstore_path: str) -> None:
    spec = ingest_openapi(petstore_path)
    spec.endpoints[0].deprecated = True
    plan = design_tools(spec)
    assert "delete_pet" not in {t.name for t in plan.tools}
    assert any("destructive" in s for s in plan.skipped)
    assert any("deprecated" in s for s in plan.skipped)
    plan = design_tools(spec, include_destructive=True, include_deprecated=False)
    delete = next(t for t in plan.tools if t.name == "delete_pet")
    assert delete.destructive


def test_flat_body_and_pagination(petstore_path: str) -> None:
    plan = design_tools(ingest_openapi(petstore_path))
    create = next(t for t in plan.tools if t.name == "create_pet")
    assert set(create.input_schema["properties"]) == {"name", "tag"}
    assert create.input_schema["required"] == ["name"]
    assert create.operations[0].body_mode == "flat"
    listing = next(t for t in plan.tools if t.name == "list_pets")
    assert listing.operations[0].page_param == "page"
    assert "all_pages" in listing.input_schema["properties"]
    assert "(GET /pets)" in listing.description


def test_wrapped_body_when_names_clash() -> None:
    spec = APISpec(
        title="t",
        endpoints=[
            Endpoint(
                method="put",
                path="/items/{id}",
                parameters=[Parameter(name="id", location=ParamLocation.PATH, required=True)],
                request_body=RequestBody(
                    required=True,
                    json_schema={"type": "object", "properties": {"id": {"type": "string"}}},
                ),
            )
        ],
    )
    [tool] = design_tools(spec).tools
    assert tool.operations[0].body_mode == "wrapped"
    assert tool.input_schema["required"] == ["id", "body"]


def test_consolidation(swagger_path: str) -> None:
    plan = design_tools(ingest_openapi(swagger_path), consolidate=True)
    manage = next(t for t in plan.tools if t.name == "manage_notes")
    assert manage.input_schema["properties"]["action"]["enum"] == ["list_notes", "create_note"]
    assert manage.input_schema["required"] == ["action"]
    assert [o.required_args for o in manage.operations] == [[], ["text"]]


def test_polish_descriptions_keeps_http_signature(petstore_path: str) -> None:
    plan = design_tools(ingest_openapi(petstore_path))

    def responder(system, prompt, output):
        return {
            "tools": [
                {"name": t.name, "description": f"Better text for {t.name}."} for t in plan.tools
            ]
        }

    polish_descriptions(plan, ScriptedLLM(responder=responder))
    get_pet = next(t for t in plan.tools if t.name == "get_pet")
    assert get_pet.description == "Better text for get_pet. (GET /pets/{petId})"
