import httpx
import pytest

from web_shooter.ingest import SpecError, ingest
from web_shooter.ingest.html_docs import (
    Chunk,
    ExtractedAPIInfo,
    ExtractedEndpoint,
    ExtractedParam,
    ExtractionResult,
    Page,
    chunk_page,
    crawl,
    html_to_markdown,
    ingest_html_docs,
    to_endpoint,
)
from web_shooter.llm import ScriptedLLM

INDEX = """
<html><body><nav><a href="/other">nav link</a></nav>
<main>
<h1>Weather API</h1>
<p>Base URL: https://api.weather.test/v2. Send your key in the <code>X-Key</code> header.</p>
<a href="/docs/forecast">Forecast</a> <a href="https://elsewhere.test/x">external</a>
</main><script>var x = 1;</script></body></html>
"""

FORECAST = """
<html><body><main>
<h2>Get forecast</h2>
<p><code>GET /forecast/{city}</code> returns the forecast for a city.</p>
<p>Query parameter <code>days</code> (integer, optional): number of days.</p>
<h2>Create alert</h2>
<p><code>POST /alerts</code> creates a weather alert. JSON body with <code>city</code>.</p>
</main></body></html>
"""


def _transport() -> httpx.MockTransport:
    pages = {"/docs/": INDEX, "/docs/forecast": FORECAST}

    def handler(request: httpx.Request) -> httpx.Response:
        html = pages.get(request.url.path)
        if html is None:
            return httpx.Response(404)
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    return httpx.MockTransport(handler)


def test_html_to_markdown_drops_noise() -> None:
    md = html_to_markdown(INDEX)
    assert "# Weather API" in md
    assert "var x" not in md and "nav link" not in md


def test_crawl_stays_in_docs_section() -> None:
    client = httpx.Client(transport=_transport())
    pages = crawl("https://docs.weather.test/docs/", client=client)
    assert [p.url for p in pages] == [
        "https://docs.weather.test/docs/",
        "https://docs.weather.test/docs/forecast",
    ]


def test_chunk_page_one_chunk_per_endpoint() -> None:
    chunks = chunk_page(Page(url="u", markdown=html_to_markdown(FORECAST)))
    assert len(chunks) == 2
    assert "GET /forecast/{city}" in chunks[0].text
    assert "POST /alerts" in chunks[1].text


def _endpoint(**kw) -> ExtractedEndpoint:
    base = dict(
        method="GET",
        path="/forecast/{city}",
        summary="Get forecast",
        description="",
        parameters=[
            ExtractedParam(
                name="days", location="query", required=False, type="integer", description=""
            )
        ],
        body_content_type="",
        body_schema_json="",
        response_schema_json="",
        evidence="GET /forecast/{city} returns the forecast",
        confidence=0.9,
        missing=[],
    )
    base.update(kw)
    return ExtractedEndpoint(**base)


def test_to_endpoint_adds_path_params_and_flags_missing_schema() -> None:
    chunk = Chunk(url="u", text="`GET /forecast/{city}` returns the forecast for a city.")
    e = to_endpoint(_endpoint(evidence="GET /forecast/{city} returns the forecast"), chunk)
    names = {(p.name, p.location.value, p.required) for p in e.parameters}
    assert ("city", "path", True) in names
    assert "response schema" in e.missing
    assert e.confidence == 0.9
    assert e.sources[0].url == "u"


def test_to_endpoint_grounding_check_lowers_confidence() -> None:
    chunk = Chunk(url="u", text="GET /forecast/{city} returns the forecast")
    e = to_endpoint(_endpoint(path="/invented", evidence="made up text"), chunk)
    assert e.confidence <= 0.3
    assert "evidence not found verbatim in source" in e.missing
    assert "path not found in source" in e.missing


def test_ingest_html_docs_end_to_end_with_scripted_llm() -> None:
    def responder(system, prompt, output):
        if output is ExtractedAPIInfo:
            return ExtractedAPIInfo(
                title="Weather API",
                description="Forecasts",
                base_url="https://api.weather.test/v2",
                auth_type="api_key",
                auth_param_name="X-Key",
                auth_location="header",
            )
        assert output is ExtractionResult
        if "GET /forecast/{city}" in prompt:
            return ExtractionResult(endpoints=[_endpoint()])
        if "POST /alerts" in prompt:
            return ExtractionResult(
                endpoints=[
                    _endpoint(
                        method="POST",
                        path="/alerts",
                        summary="Create alert",
                        parameters=[],
                        body_content_type="application/json",
                        body_schema_json='{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}',
                        evidence="POST /alerts creates a weather alert",
                    )
                ]
            )
        return ExtractionResult(endpoints=[])

    llm = ScriptedLLM(responder=responder)
    spec = ingest_html_docs(
        "https://docs.weather.test/docs/", llm=llm, client=httpx.Client(transport=_transport())
    )
    assert spec.base_url == "https://api.weather.test/v2"
    assert {e.key for e in spec.endpoints} == {"GET /forecast/{city}", "POST /alerts"}
    alerts = next(e for e in spec.endpoints if e.path == "/alerts")
    assert alerts.request_body.json_schema["required"] == ["city"]
    assert spec.auth_schemes[0].param_name == "X-Key"
    assert all(e.auth == ["auth"] for e in spec.endpoints)
    assert llm.usage.calls >= 3


def test_ingest_html_without_llm_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import web_shooter.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "load_document", lambda source: ({"html": True}, ""))
    with pytest.raises(SpecError, match="LLM"):
        ingest("https://docs.weather.test/docs/", llm=None)
