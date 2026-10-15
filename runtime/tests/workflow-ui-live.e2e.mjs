// Real HTTP + SQLite browser acceptance. Explicit isolated fixture opt-in required.
// The supplied serve_workflow_fixture.py replaces only the model boundary.
import assert from "node:assert/strict";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const base = process.env.WORKFLOW_LIVE_BASE;
if (process.env.WORKFLOW_FIXTURE_CONFIRM !== "1" || !base || !["127.0.0.1", "localhost"].includes(new URL(base).hostname)) throw new Error("Explicit loopback fixture required.");
const token = (await readFile(process.env.WORKFLOW_TOKEN_FILE, "utf8")).trim();
const output = process.env.WORKFLOW_SCREENSHOT_DIR;
const suffix = Date.now().toString(36), project = `ui-live-${suffix}`, relay = `ui-relay-${suffix}`, source = `ui-source-${suffix}`;
async function request(path, method = "GET", body) {
  const response = await fetch(`${base}${path}`, { method, headers: { Authorization: `Bearer ${token}`, Origin: base, ...(body ? { "Content-Type": "application/json" } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
  const data = await response.json(); assert.ok(response.ok, `${response.status} ${JSON.stringify(data)}`); return data;
}
await request("/v1/messages", "POST", { project_id: project, session_id: `browser-acceptance-${suffix}`, role: "user", content: "Browser fixture: the fixed dependency mirror failed; retry only when its availability changes.", source_app: "workflow-browser-test", native_message_id: source, message_id: source });
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } }); const errors = [], actions = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", async (response) => {
    if (response.url().endsWith("/workflows") && response.request().method() === "POST") {
      const body = response.request().postDataJSON(); actions.push({ action: body.action, status: response.status() });
    }
  });
  await page.goto(`${base}/workflows/workflow-center.html`);
  async function settle() { await page.waitForFunction(() => !document.body.hasAttribute("aria-busy")); assert.equal(await page.locator("#status").getAttribute("class"), "success", await page.locator("#status").textContent()); }
  async function connect(id) { await page.locator("#project").fill(id); await page.locator("#token").fill(token); await page.locator("#connect").click(); await settle(); }
  async function form(id, values) {
    await page.locator(`#${id}`).evaluate((node) => { const details = node.closest("details"); if (details) details.open = true; });
    for (const [name, value] of Object.entries(values)) {
      const control = page.locator(`#${id} [name="${name}"]`);
      if (await control.evaluate((el) => el.tagName === "SELECT")) await control.selectOption(value); else await control.fill(value);
    }
    await page.locator(`#${id} button[type=submit]`).click(); await settle();
  }
  await connect(project);
  await form("task-form", { id: "release", title: "浏览器验收任务 <img src=x onerror=alert(1)>", goal: "证明真实 API 可接续", next_step: "检查实际退出码", version: "fixture-v1", evidence_ids: source });
  await form("receipt-form", { id: "release", version: "fixture-v1", outcome: "passed", summary: "用户报告测试通过，不等于实际执行验证", evidence_ids: source });
  await page.locator("#task-list").getByText("仅人工 / Agent 报告", { exact: true }).waitFor();
  assert.equal(await page.locator("#task-list img").count(), 0);
  await form("attempt-form", { id: "failed-build", task_id: "release", config: '{"version":"fixture-v1","mirror":"a"}', summary: "依赖镜像返回 502", evidence_ids: source, retry_conditions: "镜像可用性改变" });
  await form("decision-form", { id: "decision-a", title: "缓存方案", text: "先禁用缓存", evidence_ids: source });
  await form("decision-form", { id: "decision-b", title: "部署策略", text: "依据缓存方案部署", depends_on: "decision-a", evidence_ids: source });
  await form("revision-form", { id: "decision-a", text: "新证据支持开启缓存", reason: "新增隔离压测", evidence_ids: source });
  await page.locator("#decision-list").getByText("依赖变更，待复查", { exact: true }).waitFor();
  await form("reminder-form", { id: "publish-check", title: "发布前检查真实结果", reason: "当前仅有报告，未核验执行", evidence_ids: source, query_terms: "发布", cooldown_seconds: "1" });
  await form("evaluate-form", { event: "question", question: "可以发布吗", task_id: "release", config: '{"version":"fixture-v1","mirror":"a"}' });
  await page.locator("#reminder-result").getByText("重试条件：镜像可用性改变", { exact: true }).waitFor();
  await page.locator("#reminder-result .record").filter({ hasText: "发布前检查真实结果" }).getByText("延后一小时", { exact: true }).click(); await settle();
  await page.locator("#reminder-result .record").filter({ hasText: "Previous failure with the same configuration" }).getByText("忽略", { exact: true }).click(); await settle();
  let snap = await request(`/v1/projects/${project}/workflows`);
  assert.equal(snap.tasks[0].verified, false); assert.equal(snap.tasks[0].verification_state, "reported");
  assert.equal(snap.decisions.find((d) => d.id === "decision-b").needs_review, true);
  assert.equal(snap.reminders.find((r) => r.id === "publish-check").snoozed_until !== null, true);
  await page.locator('input[name="export-record"][value="decision:decision-a"]').check(); await form("export-form", {});
  const knowledge = JSON.parse(await page.locator("#export-preview").textContent()); assert.equal(knowledge.items.length, 1); assert.equal(knowledge.items[0].kind, "knowledge");
  const downloadPromise = page.waitForEvent("download"); await page.locator("#export-download").click(); const download = await downloadPromise;
  if (output) { await mkdir(output, { recursive: true }); await download.saveAs(join(output, "selected-knowledge-package.json")); }
  await page.locator("#export-mode").selectOption("selected_sources"); for (const choice of await page.locator('input[name="export-record"]').all()) await choice.uncheck(); await page.locator(`input[name="export-record"][value="${source}"]`).check(); await form("export-form", {});
  const sourcePackage = JSON.parse(await page.locator("#export-preview").textContent()); assert.equal(sourcePackage.items.length, 1);
  if (output) {
    await page.screenshot({ path: join(output, "live-workflow-desktop.png"), fullPage: true });
    for (const section of ["tasks", "decisions", "reminders", "bridge"]) await page.locator(`#${section}`).screenshot({ path: join(output, `live-panel-${section}.png`) });
  }
  await connect(relay);
  async function importPackage(value, filename) {
    await page.locator("#import-file").setInputFiles({ name: filename, mimeType: "application/json", buffer: Buffer.from(JSON.stringify(value)) });
    const choices = page.locator('input[name="import-record"]'); await choices.first().waitFor();
    assert.equal(await choices.first().isChecked(), false);
    for (const choice of await choices.all()) await choice.check();
    await page.locator("#import-preview-button").click(); await settle();
    assert.equal(await page.locator("#import-commit").isDisabled(), true);
    const preview = JSON.parse(await page.locator("#import-preview").textContent()); assert.equal(preview.can_commit, true);
    await page.locator("#import-confirm").check(); await page.locator("#import-commit").click(); await settle();
    return { preview, result: JSON.parse(await page.locator("#import-result").textContent()) };
  }
  const imported = await importPackage(sourcePackage, "selected-source.json");
  assert.equal(imported.result.status, "applied"); assert.equal(imported.result.indexed, true);
  const importedId = imported.result.records[0].receipt.local_message_id;
  let messages = (await request(`/v1/messages?project_id=${relay}`)).messages;
  assert.ok(messages.some((m) => m.message_id === importedId && m.content.includes("external_unverified")));
  // A selected deletion is an API supplement to the existing memory screen, not an implicit UI operation.
  await request(`/v1/messages/${source}`, "DELETE"); await connect(project);
  await page.locator("#revocation-form").evaluate((node) => { node.closest("details").open = true; });
  await page.locator("#revocation-list label").filter({ hasText: source }).locator("input").check(); await form("revocation-form", {});
  const revocationPackage = JSON.parse(await page.locator("#export-preview").textContent()); assert.equal(revocationPackage.items[0].kind, "revocation");
  await connect(relay);
  const revoked = await importPackage(revocationPackage, "revocation.json");
  assert.ok(revoked.preview.revocations[0].targets.some((target) => target.local_message_id === importedId));
  assert.equal(revoked.result.status, "applied");
  messages = (await request(`/v1/messages?project_id=${relay}`)).messages;
  assert.ok(!messages.some((m) => m.message_id === importedId));
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  if (output) await page.screenshot({ path: join(output, "live-workflow-mobile.png"), fullPage: true });
  assert.deepEqual(errors, []);
  assert.deepEqual(await page.evaluate(() => [localStorage.length, sessionStorage.length]), [0, 0]);
  assert.equal(await page.locator("#token").inputValue(), ""); assert.ok(!page.url().includes(token));
  const report = { kind: "real_loopback_api_browser_acceptance", model_boundary: "FakeMemoryModel", persistence: "real SQLite + ordinary ingestion", project, relay, actions, results: { task_report_not_execution: true, dependent_decision_marked: true, same_config_failure_reminder: true, snooze_and_ignore: true, explicit_export_preview_download: true, two_phase_import_indexed: true, explicit_revocation_deleted_target: true, xss_text_only: true, token_not_persisted: true, mobile_no_overflow: true, no_page_errors: true } };
  if (output) await writeFile(join(output, "live-browser-report.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally { await browser.close(); }
