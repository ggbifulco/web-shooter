---
description: Generate a tested MCP server from an API docs URL or OpenAPI spec and register it in Claude Code
argument-hint: <docs-url-or-openapi-spec> [extra web-shooter flags]
allowed-tools: Bash(uvx:*), Bash(web-shooter:*), Read
---

Generate an MCP server for this API with web-shooter: $ARGUMENTS

Steps:

1. Run a dry run first and show the user the planned tools:
   `uvx --from git+https://github.com/ggbifulco/web-shooter web-shooter shoot $ARGUMENTS --dry-run`
2. If the plan looks reasonable, generate, test and register the server in this project:
   `uvx --from git+https://github.com/ggbifulco/web-shooter web-shooter shoot $ARGUMENTS --out .web-shooter --install-claude project`
3. Report the result: number of tools, test summary, where the server was written, and which
   environment variables the user must set for credentials (they are listed in the generated
   server's README.md). Remind the user to restart Claude Code (or run /mcp) to load the server.

If the source is an HTML docs page rather than a spec, web-shooter needs `ANTHROPIC_API_KEY`
in the environment. If tests fail, show the first failures from `test_report.json` and do not
claim the server works.
