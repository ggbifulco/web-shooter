---
name: web-shooter
description: Use when the user wants Claude to work with an HTTP API that has no MCP server yet ("connect to this API", "make tools for this API", "here is the OpenAPI spec / API docs"). web-shooter turns an OpenAPI/Swagger spec or HTML API docs into a tested MCP server and registers it in Claude Code.
---

# web-shooter

web-shooter generates a standalone MCP server from an API description, tests every tool in a
sandbox, repairs failures with an LLM, and can register the result in Claude Code.

## When to use

- The user gives an OpenAPI/Swagger URL or file, or a link to API documentation, and wants
  Claude to call that API.
- An API is used repeatedly in the project and calling it through ad-hoc `curl` is error-prone.

## How

Prefer the `/web-shooter <source>` command. Without the plugin command, run:

```bash
uvx --from git+https://github.com/ggbifulco/web-shooter web-shooter shoot <source> --dry-run
uvx --from git+https://github.com/ggbifulco/web-shooter web-shooter shoot <source> --out .web-shooter --install-claude project
```

Useful flags:

| Flag | Effect |
|---|---|
| `--dry-run` | Show planned tools, write nothing |
| `--include-destructive` | Also expose DELETE endpoints (off by default) |
| `--consolidate` | One tool per resource with an `action` argument (fewer tools for big APIs) |
| `--polish` | Rewrite tool descriptions with Claude |
| `--live` | Run tests against the real API instead of mock data |
| `--sandbox docker` | Run tests in a locked-down container |

## Things to tell the user

- Credentials are read from environment variables named in the generated README
  (for example `PETSTORE_API_KEY`); never write keys into files.
- DELETE endpoints are skipped unless asked for.
- HTML docs ingestion needs `ANTHROPIC_API_KEY`; OpenAPI specs work without any key.
- After registering, Claude Code must reload MCP servers (restart or `/mcp`).
