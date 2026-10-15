// Reproducible browser contract test: real DOM/clicks against an explicitly mocked API.
// Run: PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs node runtime/tests/workflow-ui.e2e.mjs
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const here = dirname(fileURLToPath(import.meta.url));
const ui = join(here, "../src/tmcra_local/ui");
const requests = [], snapshot = { revision: 1, tasks: [], attempts: [], decisions: [], reminders: [], bridge_plans: [], available_records: [{ id: "knowledge-1", kind: "knowledge", title: "已确认的项目知识" }] };
const pkg = { schema_version: "tmcra-memory-package-v1", items: [{ id: "item-1", kind: "knowledge", text: "导入内容 <img src=x onerror=alert(1)>", provenance: "external_unverified" }] };
let plan = null;
function send(res, data, code = 200) { res.writeHead(code, { "content-type": "application/json" }); res.end(JSON.stringify(data)); }
const server = createServer(async (req, res) => {
  try {
    if (req.url.startsWith("/v1/")) {
      if (req.headers.authorization !== "Bearer ui-test-token") return send(res, { error: "bad token" }, 401);
      if (req.method === "GET") return send(res, snapshot);
      let text = ""; for await (const chunk of req) text += chunk;
      const body = JSON.parse(text); requests.push(body); const p = body.payload;
      assert.match(body.operation_id, /^[a-z\d-]+$/iu);
      let result = {};
      if (body.action === "task.upsert") snapshot.tasks.push({ ...p, verification_state: "unverified", revision: 1 });
      if (body.action === "task.receipt") snapshot.tasks[0].verification_state = "reported";
      if (body.action === "attempt.record") snapshot.attempts.push(p);
      if (body.action === "decision.create") snapshot.decisions.push({ ...p, revision: 1, history: [] });
      if (body.action === "decision.revise") { const d = snapshot.decisions.find((v) => v.id === p.id); d.history.push({ text: d.text, reason: p.reason }); d.text = p.text; d.revision += 1; }
      if (body.action === "reminder.upsert") snapshot.reminders.push(p);
      if (body.action === "reminder.evaluate") result = { reminders: snapshot.reminders.map((r) => ({ ...r, why_now: "你正在提问发布，本任务还有未核验结果。" })) };
      if (body.action === "reminder.act") snapshot.reminders.find((r) => r.id === p.id).disposition = p.disposition;
      if (body.action === "bridge.export") result = pkg;
      if (body.action === "bridge.preview") result = { preview_token: "preview-one", records: [{ item_id: "item-1", status: "pending" }], conflicts: [], blocked: [], warnings: ["external_unverified"] };
      if (body.action === "bridge.plan") { assert.deepEqual(p, {plan_id:"plan-1"}); result=plan; }
      if (body.action === "bridge.retry_failed") { assert.deepEqual(p, {plan_id:"plan-1",item_id:"item-1",confirmed:true}); result={...plan,status:"accepted_pending"}; snapshot.bridge_plans=[result]; }
      if (body.action === "bridge.commit") { assert.deepEqual(p, { preview_token: "preview-one" }); plan = { plan_id: "plan-1", status: "partial", indexed: false, records: [{ item_id: "item-1", status: "pending", indexed: false }] }; snapshot.bridge_plans = [plan]; result = plan; }
      snapshot.revision += 1; return send(res, result);
    }
    const name = req.url.split("?")[0].split("/").pop() || "workflow-center.html";
    if (!["workflow-center.html", "workflow-center.js", "workflow-center.css"].includes(name)) { res.writeHead(404); return res.end(); }
    res.setHeader("content-type", name.endsWith(".js") ? "text/javascript" : name.endsWith(".css") ? "text/css" : "text/html");
    res.end(await readFile(join(ui, name)));
  } catch (error) { send(res, { error: error.message }, 500); }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1360, height: 1000 } }); const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}/workflows/workflow-center.html`);
  await page.locator("#project").fill("ui-project"); await page.locator("#token").fill("ui-test-token"); await page.locator("#connect").click();
  await page.getByText("还没有任务，先记录一个真实目标。").waitFor();
  async function form(id, values) {
    await page.locator(`#${id}`).evaluate((node) => { const details = node.closest("details"); if (details) details.open = true; });
    for (const [name, value] of Object.entries(values)) {
      const control = page.locator(`#${id} [name="${name}"]`);
      if (await control.evaluate((el) => el.tagName === "SELECT")) await control.selectOption(value); else await control.fill(value);
    }
    await page.locator(`#${id} button[type=submit]`).click(); await page.waitForFunction(() => !document.body.hasAttribute("aria-busy"));
    assert.equal(await page.locator("#status").getAttribute("class"), "success", await page.locator("#status").textContent());
  }
  await form("task-form", { id: "task-1", title: "发布 <img src=x onerror=alert(1)>", goal: "准确接续", next_step: "检查实际结果", version: "abc123", evidence_ids: "source-a" });
  assert.equal(await page.locator("#task-list img").count(), 0);
  await form("receipt-form", { id: "task-1", version: "abc123", outcome: "passed", summary: "测试报告通过", evidence_ids: "source-a" });
  await page.getByText("仅人工 / Agent 报告", { exact: true }).waitFor();
  await form("attempt-form", { id: "attempt-1", task_id: "task-1", summary: "依赖服务返回 502", config: '{"version":"abc123"}', retry_conditions: "镜像恢复" });
  await form("decision-form", { id: "decision-1", title: "缓存策略", text: "先不启用缓存", evidence_ids: "source-a" });
  await form("revision-form", { id: "decision-1", text: "测试后开启缓存", reason: "新的压测证据", evidence_ids: "source-b" });
  await page.locator("#decision-list").getByText("测试后开启缓存", { exact: true }).waitFor();
  await form("reminder-form", { id: "reminder-1", title: "发布前核验", reason: "测试还只是报告", evidence_ids: "source-a", query_terms: "发布", cooldown_seconds: "1" });
  await form("evaluate-form", { event: "question", question: "现在可以发布吗" });
  await page.locator("#reminder-result").getByText("延后一小时", { exact: true }).click();
  await page.waitForFunction(() => !document.body.hasAttribute("aria-busy"));
  assert.equal(requests.at(-1).payload.disposition, "snooze");
  await page.locator('input[name="export-record"]').check();
  await form("export-form", {});
  assert.match(await page.locator("#export-preview").textContent(), /external_unverified/u);
  const download = page.waitForEvent("download"); await page.locator("#export-download").click(); assert.equal((await download).suggestedFilename(), "tmcra-memory-package.json");
  await page.locator("#import-file").setInputFiles({ name: "memory.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(pkg)) });
  await page.locator('input[name="import-record"]').check(); await page.locator("#import-preview-button").click();
  await page.locator("#import-confirm-label").waitFor({ state: "visible" });
  assert.equal(await page.locator("#import-commit").isDisabled(), true);
  await page.locator("#import-confirm").check(); await page.locator("#import-commit").click();
  await page.waitForFunction(() => !document.body.hasAttribute("aria-busy"));
  assert.match(await page.locator("#import-result").textContent(), /"indexed": false/u);
  assert.equal(await page.locator("#import-items img").count(), 0);
  const beforePlan = requests.filter((r) => r.action === "bridge.plan").length;
  page.once("dialog", async (dialog) => { assert.match(dialog.message(), /不是只读检查/u); assert.match(dialog.message(), /item-1/u); await dialog.dismiss(); });
  await page.getByRole("button", { name: "确认恢复未完成项", exact: true }).click();
  assert.equal(requests.filter((r) => r.action === "bridge.plan").length, beforePlan);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "确认恢复未完成项", exact: true }).click();
  await page.waitForFunction(() => !document.body.hasAttribute("aria-busy"));
  assert.equal(requests.filter((r) => r.action === "bridge.plan").length, beforePlan + 1);
  plan.status = "failed"; plan.records[0].status = "failed"; plan.records[0].engine_state = "failed";
  await page.locator("#connect").click(); await page.waitForFunction(() => !document.body.hasAttribute("aria-busy"));
  const before = requests.length;
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "申请恢复失败项：item-1", exact: true }).click();
  assert.equal(requests.length, before);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "申请恢复失败项：item-1", exact: true }).click();
  await page.waitForFunction(() => !document.body.hasAttribute("aria-busy"));
  assert.equal(requests.at(-1).action, "bridge.retry_failed");
  assert.deepEqual(requests.at(-1).payload, {plan_id:"plan-1",item_id:"item-1",confirmed:true});
  assert.deepEqual(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })), { local: 0, session: 0 });
  assert.ok(!page.url().includes("ui-test-token")); assert.equal(await page.locator("#token").inputValue(), "");
  assert.deepEqual(errors, []);
  if (process.env.WORKFLOW_SCREENSHOT_DIR) { await mkdir(process.env.WORKFLOW_SCREENSHOT_DIR, { recursive: true }); await page.screenshot({ path: join(process.env.WORKFLOW_SCREENSHOT_DIR, "workflow-center-desktop.png"), fullPage: true }); }
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  if (process.env.WORKFLOW_SCREENSHOT_DIR) await page.screenshot({ path: join(process.env.WORKFLOW_SCREENSHOT_DIR, "workflow-center-mobile.png"), fullPage: true });
  await page.locator("#disconnect").click(); assert.equal(await page.locator("#task-list").textContent(), "");
  console.log(JSON.stringify({ browser: "chromium", api: "explicit_mock", four_flows: "passed", mutation_count: requests.length, plan_confirmation: {cancel_posts:0,accept_posts:1}, xss: "passed", token_persistence: "none", responsive_width: "passed", no_page_errors: true }));
} finally { await browser.close(); server.close(); }
