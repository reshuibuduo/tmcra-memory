# TMCRA owner-local agent hooks

This package connects supported coding-agent lifecycle hooks to the loopback-only TMCRA API. It never contacts a TMCRA account service or a TMCRA-hosted endpoint.

## Stable path

Codex is the first fully installed path in this source release:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-codex-local.ps1
```

The installer stores no bearer token in the integration config. It stores the absolute path of the token file created by `install-local`, registers the repository as a local Codex marketplace, enables Hooks, and installs `tmcra-local-memory`.

Restart Codex and review the four hooks under `/hooks`. Codex requires the user to trust hooks explicitly.

## Lifecycle contract

1. `UserPromptSubmit` recalls owner-global and current-project memory for the current prompt.
2. Recalled text is injected inside an explicit untrusted-data boundary.
3. The redacted user prompt is stored as a `user` source record with `both` visibility by default.
4. `Stop` stores visible assistant output as a separate `assistant` record in the project scope.
5. Failed local writes enter an owner-local outbox and are retried by the next lifecycle event.
6. Project identity is shared across tools through `.tmcra/project.json`, Git origin, Git root, or the canonical directory path, in that order.

`claude-hooks.json` and `zcode-hooks.json` implement the same data contract. They are included for integration testing and manual host registration. This release does not claim one-click installation for those hosts until their current public packaging flows have independent acceptance tests.

## Security boundary

- API destinations must be `localhost`, `127.0.0.1`, or `::1`.
- The local bearer token is read from its file at request time and is never serialized into integration state.
- Common credentials are redacted before recall queries or message writes.
- Hook errors are fail-open for the host agent. Diagnostics contain error class and bounded messages, not prompts or responses.


## Workflow Skill and MCP

`skills/tmcra-workflows` provides resume, timeline, reminder and selective-package workflows through the actual `tmcra_workflows` MCP tool. See [MCP/API examples](docs/workflow-mcp-api.zh-CN.md). Skill files alone do not prove the client loaded the server; confirm with tools/list and a real read. Sensitive actions still require host confirmation.
