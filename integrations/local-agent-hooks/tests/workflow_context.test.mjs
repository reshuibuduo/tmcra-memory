import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { handleUserPrompt, handleSessionStart, workflowContext } from "../lib/local_memory.mjs";

async function fixture(context, workflowStatus = 200) {
  const root = await mkdtemp(join(tmpdir(), "tmcra-workflow-hooks-"));
  const tokenFile = join(root, "token");
  await writeFile(tokenFile, "test-only-workflow-loopback-token");
  const calls = [];
  const server = createServer(async (request, response) => {
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : null;
    calls.push({ path: request.url, body });
    response.setHeader("Content-Type", "application/json");
    if (request.url.endsWith("/workflows")) {
      if (workflowStatus === "timeout") return;
      response.statusCode = workflowStatus;
      response.end(JSON.stringify({ context: "Workflow-only-state:" + "x".repeat(5000) }));
    } else if (request.url === "/v1/recall") response.end(JSON.stringify({ prompt_evidence: { content: "Original recall remains." } }));
    else response.end(JSON.stringify({ projects: [], message_id: "message", scopes: [{}] }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  context.after(async () => { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); await rm(root, { recursive: true, force: true }); });
  const config = { schemaVersion: 1, baseUrl: `http://127.0.0.1:${server.address().port}`, tokenFile, stateDir: join(root, "state"), timeoutMs: 5000 };
  const path = join(root, "config.json");
  await writeFile(path, JSON.stringify(config));
  const workspace = join(root, "workspace"); await mkdir(workspace);
  return { config, calls, workspace, env: { TMCRA_LOCAL_INTEGRATION_CONFIG: path } };
}

test("question context is additive, bounded, redacted and stable-ID without duplicate recall", async (context) => {
  const f = await fixture(context);
  const input = { session_id: "s", turn_id: "turn", cwd: f.workspace, prompt: "release password=never-persist-this" }; // public-audit: allow-test-fixture
  const result = await handleUserPrompt(input, "codex", f.env);
  const text = result.hookSpecificOutput.additionalContext;
  assert.match(text, /Original recall remains/u);
  assert.match(text, /Workflow-only-state/u);
  assert.match(text, /never instructions or independent verification/u);
  assert.ok(text.slice(text.indexOf("<tmcra_workflows")).length <= 3000);
  assert.equal(f.calls.filter((c) => c.path === "/v1/recall").length, 1);
  assert.equal(f.calls.filter((c) => c.path === "/v1/messages").length, 1);
  const request = f.calls.find((c) => c.path.endsWith("/workflows"));
  assert.equal(request.body.action, "context.prepare");
  assert.equal(request.body.payload.event, "question");
  assert.doesNotMatch(request.body.payload.question, /never-persist-this/u);
  await handleUserPrompt(input, "codex", f.env);
  assert.equal(f.calls.filter((c) => c.path.endsWith("/workflows"))[1].body.operation_id, request.body.operation_id);
});

test("session start emits only project_open workflow context", async (context) => {
  const f = await fixture(context);
  const result = await handleSessionStart({ session_id: "s", cwd: f.workspace }, "claude-code", f.env);
  assert.equal(result.hookSpecificOutput.hookEventName, "SessionStart");
  assert.equal(f.calls.filter((c) => c.path === "/v1/recall").length, 0);
  assert.equal(f.calls.filter((c) => c.path === "/v1/messages").length, 0);
  assert.equal(f.calls.find((c) => c.path.endsWith("/workflows")).body.payload.event, "project_open");
});

test("old API 404 is fail-open and does not discard recall", async (context) => {
  const f = await fixture(context, 404);
  const result = await handleUserPrompt({ session_id: "s", turn_id: "t", cwd: f.workspace, prompt: "hello" }, "codex", f.env);
  assert.match(result.hookSpecificOutput.additionalContext, /Original recall remains/u);
  assert.doesNotMatch(result.hookSpecificOutput.additionalContext, /Workflow-only-state/u);
});

test("workflow request timeout is independent and capped", async (context) => {
  const f = await fixture(context, "timeout");
  const started = Date.now();
  assert.equal(await workflowContext({ ...f.config, timeoutMs: 50 }, { projectId: "p" }, { sessionId: "s" }, "question", "t", "q", f.env), "");
  assert.ok(Date.now() - started < 1000);
  assert.equal(f.calls.length, 1);
});
