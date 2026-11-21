# Launch post (LinkedIn / blog draft)

**I built a tool that turns any API into a tested MCP server in seconds. 🕸️**

Most APIs still don't ship an MCP server, so assistants like Claude can't use them as tools.
Writing one by hand means mapping endpoints to tools, handling auth, pagination and errors, and
then hoping it works.

**web-shooter** does it automatically:

1. **Ingest:** reads an OpenAPI/Swagger spec, or crawls plain HTML docs and extracts endpoints with
   Claude (structured outputs + a grounding check: every endpoint must cite a verbatim excerpt
   of the docs, or its confidence drops).
2. **Design:** picks good tool names, writes descriptions and JSON schemas, skips destructive
   endpoints unless asked, and can merge CRUD endpoints into one tool.
3. **Generate:** a standalone Python package on the official MCP SDK, with Dockerfile.
4. **Test:** every tool gets a valid call (response validated against the schema) and two invalid
   calls that must be rejected, run in a local or locked-down Docker sandbox.
5. **Repair:** failing tests go back to the LLM, which answers with minimal patches, until green.
6. **Ship:** one flag registers the server in Claude Code; there is also a Claude Code plugin.

**Benchmark on 16 real public API specs** (Spotify, Gitea, httpbin, Wikimedia, CircleCI…):
**14/16 fully green, 1,489/1,494 generated tests passing**, about 2 seconds per API. The five
failures are contradictions inside the specs themselves, which the tests surface.

The benchmark paid for itself: running it found four real bugs in my own code, all fixed with
regression tests.

What I learned:

- "It compiles" is not a quality bar for generated code. Generated tests are what make
  codegen trustworthy.
- Real-world specs are messy: recursive `$ref`s, `allOf` parts that contradict each other,
  `required` fields that are never defined. A benchmark on real data beats any hand-written
  fixture.
- LLM extraction needs grounding. Asking the model to quote its source and verifying the quote
  is a cheap, effective guard against invented endpoints.

Code (MIT): https://github.com/ggbifulco/web-shooter

#AI #LLM #MCP #Claude #AIEngineering #OpenSource
