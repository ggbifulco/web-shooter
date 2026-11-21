"""Ingest human-readable API docs (HTML) with an LLM.

Pipeline: crawl same-site pages -> clean HTML to markdown -> split into chunks that
each describe (ideally) one endpoint -> structured LLM extraction into the IR, keeping
the source URL and a verbatim excerpt for every endpoint.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify
from pydantic import BaseModel, Field

from web_shooter.ir import (
    APISpec,
    AuthScheme,
    AuthType,
    Endpoint,
    Parameter,
    ParamLocation,
    RequestBody,
    Response,
    Source,
)
from web_shooter.llm import LLM

ENDPOINT_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+`?(/[^\s`'\"<>)]*)", re.IGNORECASE)
MAX_CHUNK_CHARS = 6000


# ---------------------------------------------------------------- crawling


@dataclass
class Page:
    url: str
    markdown: str


def _same_section(url: str, root: str) -> bool:
    u, r = urlparse(url), urlparse(root)
    root_dir = r.path if r.path.endswith("/") else r.path.rsplit("/", 1)[0] + "/"
    return u.netloc == r.netloc and u.path.startswith(root_dir)


def html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    md = markdownify(str(main), heading_style="ATX", code_language="")
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def crawl(
    start_url: str,
    max_pages: int = 30,
    max_depth: int = 2,
    client: httpx.Client | None = None,
) -> list[Page]:
    """Breadth-first crawl of docs pages under the start URL's directory."""
    own_client = client is None
    client = client or httpx.Client(timeout=20.0, follow_redirects=True)
    pages: list[Page] = []
    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    try:
        while queue and len(pages) < max_pages:
            url, depth = queue.popleft()
            url = urldefrag(url)[0]
            if url in seen:
                continue
            seen.add(url)
            try:
                resp = client.get(url)
            except httpx.HTTPError:
                continue
            if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "html"):
                continue
            pages.append(Page(url=url, markdown=html_to_markdown(resp.text)))
            if depth >= max_depth:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                link = urldefrag(urljoin(url, str(a["href"])))[0]
                if link not in seen and _same_section(link, start_url):
                    queue.append((link, depth + 1))
    finally:
        if own_client:
            client.close()
    return pages


# ---------------------------------------------------------------- chunking


@dataclass
class Chunk:
    url: str
    text: str


def chunk_page(page: Page) -> list[Chunk]:
    """Split a page at headings; keep sections that mention an endpoint signature.

    If no section names an endpoint, the page is kept whole (truncated) so the LLM can
    still find endpoints written in an unusual format.
    """
    sections = re.split(r"\n(?=#{1,4} )", page.markdown)
    chunks: list[Chunk] = []
    current = ""
    for section in sections:
        has_endpoint = bool(ENDPOINT_RE.search(section))
        if has_endpoint:
            if current and ENDPOINT_RE.search(current):
                chunks.append(Chunk(page.url, current.strip()))
                current = ""
            current += "\n" + section
        elif current:
            current += "\n" + section
        if len(current) > MAX_CHUNK_CHARS:
            chunks.append(Chunk(page.url, current.strip()[:MAX_CHUNK_CHARS]))
            current = ""
    if current.strip() and ENDPOINT_RE.search(current):
        chunks.append(Chunk(page.url, current.strip()))
    if not chunks and page.markdown.strip():
        chunks.append(Chunk(page.url, page.markdown[:MAX_CHUNK_CHARS]))
    return chunks


# ---------------------------------------------------------------- extraction

Location = Literal["path", "query", "header", "cookie"]


class ExtractedParam(BaseModel):
    name: str
    location: Location
    required: bool
    type: str = Field(description="JSON Schema type: string, integer, number, boolean, array")
    description: str


class ExtractedEndpoint(BaseModel):
    method: str = Field(description="HTTP method, uppercase")
    path: str = Field(description="Path template starting with /, params as {name}")
    summary: str
    description: str
    parameters: list[ExtractedParam]
    body_content_type: str = Field(description="Empty string if there is no request body")
    body_schema_json: str = Field(description="JSON Schema of the body as a JSON string, or ''")
    response_schema_json: str = Field(description="JSON Schema of a 2xx response as JSON, or ''")
    evidence: str = Field(description="Short verbatim excerpt from the docs naming this endpoint")
    confidence: float = Field(description="0..1, how sure you are this is accurate")
    missing: list[str] = Field(description="Information the docs did not provide")


class ExtractionResult(BaseModel):
    endpoints: list[ExtractedEndpoint]


class ExtractedAPIInfo(BaseModel):
    title: str
    description: str
    base_url: str = Field(description="Absolute base URL for requests, or '' if unknown")
    auth_type: Literal["none", "api_key", "bearer", "basic", "oauth2"]
    auth_param_name: str = Field(description="Header/query name carrying an API key, or ''")
    auth_location: Literal["header", "query", "none"]


EXTRACT_SYSTEM = (
    "You extract HTTP API endpoints from documentation into structured data. "
    "Only report endpoints the text actually documents; never invent paths or parameters. "
    "If something is not stated, leave it empty and list it under 'missing'. "
    "The 'evidence' field must be copied verbatim from the documentation."
)

INFO_SYSTEM = (
    "You read the overview of an API's documentation and report its name, base URL and "
    "authentication method. Use only what the text states."
)


def _schema(text: str) -> dict[str, object] | None:
    if not text.strip():
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _normalize(text: str) -> str:
    # Ignore markdown formatting, so evidence copied with or without backticks matches.
    text = re.sub(r"[`*_]", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def to_endpoint(e: ExtractedEndpoint, chunk: Chunk) -> Endpoint:
    params = [
        Parameter(
            name=p.name,
            location=ParamLocation(p.location),
            required=p.required or p.location == "path",
            description=p.description,
            json_schema={"type": p.type or "string"},
        )
        for p in e.parameters
    ]
    # Path params mentioned in the template but not described still need a parameter.
    named = {p.name for p in params}
    for name in re.findall(r"{(\w+)}", e.path):
        if name not in named:
            params.append(Parameter(name=name, location=ParamLocation.PATH, required=True))

    body_schema = _schema(e.body_schema_json)
    body = None
    if e.body_content_type or body_schema:
        body = RequestBody(
            content_type=e.body_content_type or "application/json",
            json_schema=body_schema or {"type": "object"},
        )
    resp_schema = _schema(e.response_schema_json)

    missing = list(e.missing)
    confidence = max(0.0, min(1.0, e.confidence))
    # Grounding check: the evidence must really appear in the source chunk.
    if not e.evidence or _normalize(e.evidence) not in _normalize(chunk.text):
        missing.append("evidence not found verbatim in source")
        confidence = min(confidence, 0.4)
    if e.path not in chunk.text and e.path.split("{")[0].rstrip("/") not in chunk.text:
        missing.append("path not found in source")
        confidence = min(confidence, 0.3)
    if resp_schema is None:
        missing.append("response schema")

    return Endpoint(
        method=e.method.lower(),
        path=e.path if e.path.startswith("/") else "/" + e.path,
        summary=e.summary,
        description=e.description,
        parameters=params,
        request_body=body,
        responses=[Response(status="200", json_schema=resp_schema)],
        sources=[Source(url=chunk.url, excerpt=e.evidence[:300])],
        confidence=round(confidence, 2),
        missing=sorted(set(missing)),
    )


def extract_endpoints(chunk: Chunk, llm: LLM) -> list[Endpoint]:
    prompt = (
        f"Documentation page: {chunk.url}\n\n<docs>\n{chunk.text}\n</docs>\n\n"
        "List every HTTP endpoint documented above."
    )
    result = llm.generate(system=EXTRACT_SYSTEM, prompt=prompt, output=ExtractionResult)
    return [to_endpoint(e, chunk) for e in result.endpoints]


def extract_info(pages: list[Page], llm: LLM) -> ExtractedAPIInfo:
    overview = "\n\n".join(f"## Page {p.url}\n{p.markdown[:3000]}" for p in pages[:3])
    return llm.generate(system=INFO_SYSTEM, prompt=overview, output=ExtractedAPIInfo)


def _merge(endpoints: list[Endpoint]) -> list[Endpoint]:
    merged: dict[str, Endpoint] = {}
    for e in endpoints:
        prev = merged.get(e.key)
        if prev is None:
            merged[e.key] = e
            continue
        # Keep the more confident version, accumulate sources.
        best, other = (e, prev) if e.confidence > prev.confidence else (prev, e)
        best.sources = best.sources + [s for s in other.sources if s not in best.sources]
        merged[e.key] = best
    return list(merged.values())


def ingest_html_docs(
    url: str,
    llm: LLM,
    max_pages: int = 30,
    client: httpx.Client | None = None,
) -> APISpec:
    pages = crawl(url, max_pages=max_pages, client=client)
    if not pages:
        raise ValueError(f"could not fetch any docs page from {url}")
    info = extract_info(pages, llm)
    endpoints: list[Endpoint] = []
    for page in pages:
        for chunk in chunk_page(page):
            endpoints.extend(extract_endpoints(chunk, llm))

    schemes: list[AuthScheme] = []
    if info.auth_type != "none":
        loc = ParamLocation(info.auth_location) if info.auth_location != "none" else None
        schemes.append(
            AuthScheme(
                name="auth",
                type=AuthType(info.auth_type),
                param_name=info.auth_param_name or None,
                location=loc,
            )
        )
    endpoints = _merge(endpoints)
    for e in endpoints:
        e.auth = [s.name for s in schemes]
    return APISpec(
        title=info.title or urlparse(url).netloc,
        description=info.description,
        base_url=info.base_url.rstrip("/"),
        auth_schemes=schemes,
        endpoints=endpoints,
        origin=url,
    )
