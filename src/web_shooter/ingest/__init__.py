"""Ingesters: turn an API description (spec or docs) into the IR."""

from __future__ import annotations

import re

from web_shooter.ingest.openapi import SpecError, from_openapi, is_openapi, load_document
from web_shooter.ir import APISpec
from web_shooter.llm import LLM


def ingest(source: str, llm: LLM | None = None, max_pages: int = 30) -> APISpec:
    """Detect the kind of source and run the right ingester.

    OpenAPI/Swagger documents are parsed directly. Anything else at an http(s) URL is
    treated as human-readable docs and needs an LLM.
    """
    try:
        doc, _ = load_document(source)
    except SpecError:
        doc = None
    if doc is not None and is_openapi(doc):
        return from_openapi(doc, source)
    if not re.match(r"^https?://", source):
        raise SpecError(f"{source} is not an OpenAPI/Swagger spec")
    if llm is None:
        raise SpecError("this looks like HTML docs: an LLM is needed (set ANTHROPIC_API_KEY)")
    from web_shooter.ingest.html_docs import ingest_html_docs

    return ingest_html_docs(source, llm=llm, max_pages=max_pages)


__all__ = ["SpecError", "ingest"]
