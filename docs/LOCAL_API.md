# Local API contract

Base URL: `http://127.0.0.1:2009`

All endpoints except `/v1/health` require the bearer token stored at `.tmcra/config/runtime/secrets/local-api.token`. The server accepts loopback traffic only.

## Correct turn ordering

1. The user submits the current prompt to an agent integration.
2. The integration calls `POST /v1/recall` with the current `project_id` and prompt.
3. The integration injects `prompt_evidence.content` as untrusted memory evidence before model generation.
4. After the model responds, the integration writes the user message and assistant message as two calls to `POST /v1/messages`.

Recall cannot run before the current prompt exists. User and assistant records must never be merged into one message.

## Recall

```http
POST /v1/recall
Authorization: Bearer $TMCRA_LOCAL_TOKEN
Content-Type: application/json

{
  "project_id": "project-stable-id",
  "query": "What did we decide about the retry policy?",
  "top_k": 8
}
```

The response includes:

- `resolved_scopes`: always owner-global followed by current-project.
- `hits`: ranked records with stable memory IDs.
- `evidence_windows`: source text, actor role, session, timestamp, score, and grounded derived context.
- `prompt_evidence`: a bounded text packet for injection.
- `trace`: real per-scope retrieval summaries. It is a completed-result trace, not a simulated live event stream.

## Write one source message

```http
POST /v1/messages
Authorization: Bearer $TMCRA_LOCAL_TOKEN
Content-Type: application/json

{
  "project_id": "project-stable-id",
  "project_title": "Example project",
  "session_id": "session-stable-id",
  "session_title": "Implementation",
  "role": "user",
  "content": "Use exponential backoff with a five-attempt cap.",
  "source_app": "codex",
  "native_thread_id": "native-thread-id",
  "native_message_id": "native-message-id",
  "visibility": "both"
}
```

`role` is one of `user`, `assistant`, `system`, or `tool`. `visibility` is `project`, `global`, or `both`. A session ID is permanently bound to one project. Repeating the same native message identity with identical content is idempotent; reusing it for different content is rejected.

## Inspect and delete

```http
GET /v1/messages?project_id=project-stable-id&limit=200
DELETE /v1/messages/{message_id}
DELETE /v1/projects/{project_id}
```

Message deletion removes Source plus directly grounded derivatives from every scope to which the message was written. Project deletion also removes project metadata, Personal Knowledge, provider-usage metadata, and global derivatives originating from that project. Both operations compact the SQLite files.

Deletion cannot erase external backups, snapshots, or copies retained by the user's BYOK provider.

## Graph, knowledge, and usage

```http
GET  /v1/projects/{project_id}/graph
POST /v1/projects/{project_id}/knowledge/build
GET  /v1/projects/{project_id}/knowledge
GET  /v1/usage?project_id=project-stable-id&limit=50
```

Usage records model/provider token counters and latency only. Request and response bodies are not copied into the usage ledger. `tmcra_charge` is zero in owner-local mode; any provider charge is settled directly between the user and the selected provider.


## Deleted-source replay protection

Message and project deletion preserve only hashed stable identity fences and
revocation timestamps, not source text or raw native IDs. Both the local message
ID and the existing `(source_app, native_thread_id, native_message_id)` uniqueness
key are fenced atomically with metadata deletion. A delayed hook upload that
changes only the local ID, project, or API session is rejected rather than
recreating the source. This is identity revocation, not content deduplication;
a completely new identity is not blacklisted by matching text.

Retained soft-deletions are backfilled on reopen. Already-erased pre-upgrade
identities cannot be reconstructed. Tombstones intentionally survive project
removal. Rolling back application code leaves this metadata in place, but older
code does not enforce it; never treat a source rollback as a verified data
migration. External backup deletion and multi-process admission remain separate
operational concerns.


## Workflow MCP and API

The `tmcra_workflows` MCP tool maps project-bound calls to the existing workflow API. See [the concrete four-feature MCP/HTTP examples](WORKFLOW_MCP_API.zh-CN.md) and [client invocation table](WORKFLOW_COMMANDS.zh-CN.md). Public completion receipts remain reported; sensitive exports, commits, recovery and deletion require explicit user confirmation.
