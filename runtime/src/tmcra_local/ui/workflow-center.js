const $ = (id) => document.getElementById(id);
const personal = new URLSearchParams(location.search).get("mode") === "personal";
let token = "", project = "", connected = false, busy = false, exported = null, imported = null, preview = null;
let personalProject = "global", personalProjectLabel = "全局记忆";
const pendingOperations = new Map();
const MAX_PACKAGE_BYTES = 400_000;
const list = (value) => String(value || "").split(/[,，\n]/u).map((v) => v.trim()).filter(Boolean);
const unique = (value) => [...new Set(value)];
const element = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
};
function status(message, failed = false) { $("status").textContent = message; $("status").className = failed ? "error" : "success"; }
function apiPath() { return personal ? `/api/personal/workflows?project=${encodeURIComponent(personalProject)}` : `/v1/projects/${encodeURIComponent(project)}/workflows`; }
function errorMessage(body) {
  const value = body?.detail ?? body?.error ?? body;
  if (typeof value === "string") return value;
  return value?.message || value?.code || "请求失败，请检查连接后重试。";
}
async function api(action, payload) {
  if (!connected) throw new Error("请先连接项目。");
  const init = { method: action ? "POST" : "GET", cache: "no-store", credentials: personal ? "same-origin" : "omit", redirect: "error", headers: { Accept: "application/json" } };
  if (!personal) init.headers.Authorization = `Bearer ${token}`;
  let key;
  if (action && !action.startsWith("bridge.") && Number.isInteger(snapshot.revision)) payload = { ...payload, expected_revision: snapshot.revision };
  if (action) {
    key = JSON.stringify([action, payload]);
    const operation_id = pendingOperations.get(key) || crypto.randomUUID();
    pendingOperations.set(key, operation_id);
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify({ action, payload, operation_id });
  }
  const controller = new AbortController(); init.signal = controller.signal;
  const timeout = setTimeout(() => controller.abort(), 30_000);
  try {
    const response = await fetch(apiPath(), init);
    const body = await response.json();
    if (!response.ok) throw new Error(errorMessage(body));
    if (key) pendingOperations.delete(key);
    return body;
  } finally { clearTimeout(timeout); }
}
async function run(work) {
  if (busy) return;
  busy = true;
  const buttons = [...document.querySelectorAll("button")].map((b) => [b, b.disabled]);
  buttons.forEach(([b]) => { b.disabled = true; });
  document.body.setAttribute("aria-busy", "true"); $("workspace").inert = true;
  try { await work(); }
  catch (error) { status(error.name === "AbortError" ? "请求超时。操作可能已到达服务端；重试相同内容会复用操作 ID。" : error.message, true); }
  finally { busy = false; buttons.forEach(([b, disabled]) => { b.disabled = disabled; }); document.body.removeAttribute("aria-busy"); $("workspace").inert = false; updateImportButtons(); }
}
function showJSON(id, value) { $(id).textContent = JSON.stringify(value, null, 2); $(id).hidden = false; }
function indexDescription(plan) {
  if (plan.indexed === true) return "已确认索引完成";
  if (plan.status === "applied" && plan.records?.length && plan.records.every((entry) => entry.record?.kind === "revocation")) return "撤销已执行；无需索引";
  return "尚未确认全部索引完成";
}
function idsLine(node, ids) { if (ids?.length) node.append(element("p", `证据 ID：${ids.join("、")}`, "meta")); }
function record(title, id) { const node = element("article", undefined, "record"); node.append(element("h3", title || id), element("p", `ID：${id}`, "meta")); return node; }
function badge(node, text, tone = "") { node.append(element("span", text, `badge ${tone}`)); }
function emptyList(id, entries, render, emptyMessage) {
  const target = $(id); target.replaceChildren();
  if (!entries?.length) { target.append(element("p", emptyMessage || "暂无记录。", "empty")); return; }
  entries.forEach((entry) => target.append(render(entry)));
}
function renderTask(task) {
  const node = record(task.title, task.id);
  const labels = { executed: "执行回执有效", reported: "仅人工 / Agent 报告", stale: "核验已过期或版本不符", execution_failed: "实际执行失败", unverified: "尚未核验" };
  badge(node, task.status || "todo");
  badge(node, labels[task.verification_state] || "尚未核验", task.verified === true ? "good" : "warning");
  if (task.evidence_available === false) badge(node, "来源证据已失效", "warning");
  if (task.goal) node.append(element("p", `目标：${task.goal}`));
  if (task.next_step) node.append(element("p", `下一步：${task.next_step}`));
  node.append(element("p", `版本：${task.version || "未记录"} · 更新：${task.recorded_at || "未知"}`, "meta"));
  const receipt = task.receipts?.at(-1) || task.receipt;
  if (receipt) node.append(element("p", `结果：${receipt.summary || receipt.outcome || "未说明"}`));
  if (task.receipts?.length) {
    const detail = element("details"); detail.append(element("summary", "查看核验回执与哈希"), element("pre", JSON.stringify(task.receipts, null, 2))); node.append(detail);
  }
  idsLine(node, task.evidence_ids); return node;
}
function renderAttempt(attempt) {
  const node = record(`尝试：${attempt.summary || attempt.id}`, attempt.id); badge(node, attempt.outcome, attempt.outcome === "failed" ? "warning" : "");
  node.append(element("p", `任务：${attempt.task_id}`));
  if (attempt.retry_conditions?.length) node.append(element("p", `重试条件：${attempt.retry_conditions.join("；")}`));
  const details = element("details"); details.append(element("summary", "查看配置指纹（不保存原配置）"), element("pre", attempt.config_fingerprint || JSON.stringify(attempt.config, null, 2))); node.append(details); idsLine(node, attempt.evidence_ids); return node;
}
function renderDecision(decision) {
  const node = record(decision.title, decision.id); badge(node, `版本 ${decision.revision}`);
  if (decision.needs_review) { badge(node, "依赖变更，待复查", "warning"); node.append(element("p", decision.review_reason || "所依赖的前提已更新。")); }
  node.append(element("p", decision.text));
  if (decision.depends_on?.length) node.append(element("p", `依赖：${decision.depends_on.join("、")}`, "meta"));
  idsLine(node, decision.evidence_ids);
  const history = element("details"); history.append(element("summary", "查看结论变更历史"));
  for (const item of decision.history || []) { const row = element("div", undefined, "history"); row.append(element("p", item.text || item.previous_text || JSON.stringify(item)), element("p", `原因：${item.reason || "初始记录"}`, "meta")); history.append(row); }
  node.append(history); return node;
}
function renderReminder(reminder, actionable = false) {
  const node = record(reminder.title, reminder.id);
  const snoozed = reminder.snoozed_until && Date.parse(reminder.snoozed_until) > Date.now();
  badge(node, snoozed ? "已延后" : (reminder.disposition || reminder.state || "active"));
  if (snoozed) node.append(element("p", `延后至：${reminder.snoozed_until}`, "meta"));
  node.append(element("p", `为什么：${reminder.reason || "参考下方证据"}`));
  if (reminder.why_now) node.append(element("p", `为什么现在提醒：${typeof reminder.why_now === "string" ? reminder.why_now : JSON.stringify(reminder.why_now)}`));
  if (reminder.retry_conditions?.length) node.append(element("p", `重试条件：${reminder.retry_conditions.join("；")}`));
  idsLine(node, reminder.evidence_ids);
  if (actionable) {
    const actions = element("div", undefined, "actions");
    for (const [disposition, label] of [["complete", "完成"], ["ignore", "忽略"], ["snooze", "延后一小时"]]) {
      const button = element("button", label); button.type = "button";
      button.addEventListener("click", () => run(async () => {
        const payload = { id: reminder.id, disposition }; if (disposition === "snooze") payload.snooze_seconds = 3600;
        await api("reminder.act", payload); node.remove(); await refresh(); status(`提醒已${label}。`);
      })); actions.append(button);
    }
    node.append(actions);
  }
  return node;
}
function sourceCheckbox(item, group) {
  const label = element("label", undefined, "inline");
  const checkbox = element("input"); checkbox.type = "checkbox"; checkbox.name = group; checkbox.value = item.item_id || item.id;
  const text = element("span", `${item.title || item.text?.slice(0, 140) || checkbox.value} · ${item.kind || "record"}`);
  label.append(checkbox, text); return label;
}
function renderSources(records) {
  const selected = new Set([...document.querySelectorAll('input[name="export-record"]:checked')].map((n) => n.value));
  $("source-list").replaceChildren();
  const mode = $("export-mode").value;
  const options = (records || []).filter((item) => mode !== "knowledge_only" || item.kind === "knowledge");
  for (const item of options) { const label = sourceCheckbox(item, "export-record"); label.querySelector("input").checked = selected.has(item.id); $("source-list").append(label); }
  if (!options.length) $("source-list").append(element("p", "暂无该类型记录；可填写明确的记录 ID，不会自动选择全部。", "empty"));
}
let snapshot = {};
async function refresh() {
  snapshot = await api();
  if (personal) {
    if (!Array.isArray(snapshot.workflow_projects) || snapshot.workflow_project?.id !== personalProject) throw new Error("项目绑定未确认，请重新选择项目。");
    $("personal-project").replaceChildren();
    for (const item of snapshot.workflow_projects) {
      const option = element("option", item.label); option.value = item.id; $("personal-project").append(option);
    }
    $("personal-project").value = personalProject;
    personalProjectLabel = snapshot.workflow_project.label;
  }
  emptyList("task-list", snapshot.tasks, renderTask, "还没有任务，先记录一个真实目标。");
  emptyList("attempt-list", snapshot.attempts, renderAttempt, "尚未记录尝试。相同失败条件会在情景评估时提示。");
  emptyList("decision-list", snapshot.decisions, renderDecision, "还没有决策记录。");
  emptyList("reminder-list", snapshot.reminders, (r) => renderReminder(r));
  renderSources(snapshot.available_records);
  $("revocation-list").replaceChildren();
  for (const item of snapshot.available_revocations || []) $("revocation-list").append(sourceCheckbox({ ...item, title: item.source?.id || item.id }, "revoke-record"));
  if (!snapshot.available_revocations?.length) $("revocation-list").append(element("p", "暂无已记录撤销。", "empty"));
  emptyList("bridge-plans", snapshot.bridge_plans, (plan) => {
    const node = record("导入计划", plan.plan_id || plan.id); badge(node, plan.status || "pending"); node.append(element("p", `索引状态：${indexDescription(plan)}`, "meta"));
    const detail = element("details"); detail.append(element("summary", "查看逐项结果"), element("pre", JSON.stringify(plan, null, 2))); node.append(detail);
    if (["planned", "partial", "accepted_pending"].includes(plan.status)) {
      const retry = element("button", "确认恢复未完成项"); retry.type = "button";
      retry.addEventListener("click", () => {
        const pending = (plan.records || []).filter((entry) => entry.status === "pending");
        const changes = pending.map((entry) => {
          if (entry.record?.kind !== "revocation") return `写入条目：${entry.item_id} → ${entry.local_id || "服务端绑定记录"}`;
          const targets = (plan.revocations || []).find((item) => item.item_id === entry.item_id)?.targets || [];
          return `撤销条目：${entry.item_id}；删除目标：${targets.map((target) => target.local_message_id || target.local_id).join("、") || "无现存目标（保留撤销标记）"}`;
        });
        const message = `确认恢复计划 ${plan.plan_id || plan.id}？这不是只读检查。\n${changes.length ? changes.join("\n") : "当前没有待首次接受条目；将核对原有回执。"}\n未接受项可能继续写入记忆、调用已配置模型，撤销项可能删除列出的已导入记忆；已接受的任务只查询，不自动重新提交失败任务。若只想查看状态，请取消后使用连接/刷新。`;
        if (!window.confirm(message)) return;
        run(async () => {
          const result = await api("bridge.plan", { plan_id: plan.plan_id || plan.id });
          showJSON("import-result", result); await refresh(); status("已按确认恢复计划，请检查逐项结果和索引回执；接受请求不等于已完成。");
        });
      }); node.append(retry);
    }
    for (const entry of plan.records || []) {
      if (entry.engine_state !== "failed" || entry.status !== "failed") continue;
      const retry = element("button", `申请恢复失败项：${entry.item_id}`); retry.type = "button";
      retry.addEventListener("click", () => {
        if (!window.confirm(`确认请求恢复 ${entry.item_id}？只有引擎确认旧作业可恢复才会重试；这可能再次调用已配置模型，不会另建重复记忆。`)) return;
        run(async () => {
          const result = await api("bridge.retry_failed", { plan_id: plan.plan_id || plan.id, item_id: entry.item_id, confirmed: true });
          showJSON("import-result", result); await refresh(); status("已请求引擎检查恢复条件，请查看实际逐项状态；尚未据此确认索引完成。");
        });
      }); node.append(retry);
    }
    return node;
  }, "暂无导入计划。");
  status(`已连接${personal ? `项目 ${personalProjectLabel}` : `项目 ${project}`} · 修订 ${snapshot.revision ?? "—"}。`);
}
function formData(form) { return Object.fromEntries(new FormData(form)); }
function bindForm(id, action, convert) {
  $(id).addEventListener("submit", (event) => { event.preventDefault(); const form = event.currentTarget; run(async () => {
    const payload = convert(formData(form), form); await api(action, payload); await refresh(); status("已保存。实际执行核验与人工报告保持区分。");
  }); });
}
const withEvidence = (data) => ({ ...data, evidence_ids: list(data.evidence_ids) });
bindForm("task-form", "task.upsert", (data) => ({ ...withEvidence(data), stale_after_seconds: Number(data.stale_after_seconds) }));
bindForm("receipt-form", "task.receipt", withEvidence);
bindForm("attempt-form", "attempt.record", (data) => {
  let config; try { config = JSON.parse(data.config); } catch { throw new Error("实际配置需要有效 JSON 对象。"); }
  if (!config || typeof config !== "object" || Array.isArray(config)) throw new Error("实际配置需要 JSON 对象。");
  return { ...withEvidence(data), config, retry_conditions: list(data.retry_conditions) };
});
bindForm("decision-form", "decision.create", (data) => ({ ...withEvidence(data), depends_on: list(data.depends_on) }));
bindForm("revision-form", "decision.revise", withEvidence);
bindForm("reminder-form", "reminder.upsert", (data, form) => {
  const payload = { ...withEvidence(data), cooldown_seconds: Number(data.cooldown_seconds), events: new FormData(form).getAll("events"), query_terms: list(data.query_terms) };
  if (!payload.task_id) delete payload.task_id;
  if (!payload.events.length) throw new Error("请至少选择一个触发情景。");
  return payload;
});
$("evaluate-form").addEventListener("submit", (event) => { event.preventDefault(); run(async () => {
  const payload = formData(event.currentTarget); if (!payload.task_id) delete payload.task_id;
  if (payload.config?.trim()) {
    try { payload.config = JSON.parse(payload.config); } catch { throw new Error("重试配置需要有效 JSON 对象。"); }
    if (!payload.config || typeof payload.config !== "object" || Array.isArray(payload.config)) throw new Error("重试配置需要 JSON 对象。");
  } else delete payload.config;
  const result = await api("reminder.evaluate", payload);
  emptyList("reminder-result", result.reminders, (item) => renderReminder(item, true), "当前情景没有需要打扰你的提醒。");
  if (result.retry_warnings?.length) $("reminder-result").append(element("pre", JSON.stringify(result.retry_warnings, null, 2)));
  await refresh();
}); });
function clearImportPreview() { preview = null; $("import-confirm").checked = false; $("import-preview").hidden = true; $("import-warning").hidden = true; $("import-confirm-label").hidden = true; $("import-commit").hidden = true; updateImportButtons(); }
function updateImportButtons() {
  $("import-preview-button").disabled = busy || !connected || !imported || !document.querySelector('input[name="import-record"]:checked');
  $("import-commit").disabled = busy || !preview?.preview_token || !$("import-confirm").checked || Boolean(preview.blocked?.length) || preview?.can_commit === false;
}
$("connection-form").addEventListener("submit", (event) => { event.preventDefault(); run(async () => {
  if (!personal) {
    const nextProject = $("project").value.trim(), nextToken = $("token").value.trim() || token;
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/u.test(nextProject) || !nextToken) throw new Error("请输入有效项目 ID 和本地 Token。");
    if (project !== nextProject || token !== nextToken) pendingOperations.clear();
    project = nextProject; token = nextToken; $("token").value = ""; $("token").required = false;
  }
  connected = true; exported = null; clearImportPreview(); $("export-preview").hidden = true; $("export-download").hidden = true; $("reminder-result").replaceChildren();
  try { await refresh(); $("disconnect").hidden = personal; }
  catch (error) { connected = false; token = ""; $("token").required = !personal; throw error; }
}); });
$("disconnect").addEventListener("click", () => {
  token = ""; connected = false; $("token").required = true; pendingOperations.clear(); exported = null; imported = null; snapshot = {}; clearImportPreview();
  for (const id of ["task-list", "attempt-list", "decision-list", "reminder-list", "reminder-result", "source-list", "import-items", "bridge-plans", "revocation-list"]) $(id).replaceChildren();
  for (const id of ["export-preview", "export-download", "import-result", "disconnect"]) $(id).hidden = true;
  $("import-file").value = ""; $("import-file-status").textContent = ""; status("已断开，Token 与当前页面记录已清除。");
});
$("project").addEventListener("input", () => {
  if (connected && $("project").value.trim() !== project) {
    connected = false; clearImportPreview(); status("项目已改变，请先连接目标项目再操作。", true);
  }
});
$("personal-project").addEventListener("change", () => run(async () => {
  const selection = $("personal-project").value;
  if (!/^(global|p-[a-f0-9]{32})$/u.test(selection)) throw new Error("请选择服务端列出的项目。");
  personalProject = selection; pendingOperations.clear(); exported = null; imported = null; snapshot = {}; clearImportPreview();
  for (const id of ["task-list", "attempt-list", "decision-list", "reminder-list", "reminder-result", "source-list", "import-items", "bridge-plans", "revocation-list"]) $(id).replaceChildren();
  for (const id of ["export-preview", "export-download", "import-result"]) $(id).hidden = true;
  for (const id of ["task-form", "receipt-form", "attempt-form", "decision-form", "revision-form", "reminder-form", "evaluate-form", "export-form", "revocation-form"]) $(id).reset();
  $("import-file").value = ""; $("import-file-status").textContent = "";
  connected = true;
  try { await refresh(); } catch (error) { connected = false; throw error; }
}));
$("export-form").addEventListener("input", () => { exported = null; $("export-download").hidden = true; $("export-preview").hidden = true; });
$("export-mode").addEventListener("change", () => { exported = null; $("export-download").hidden = true; $("export-preview").hidden = true; renderSources(snapshot.available_records); });
$("export-form").addEventListener("submit", (event) => { event.preventDefault(); run(async () => {
  const data = formData(event.currentTarget); const selected_ids = unique([...document.querySelectorAll('input[name="export-record"]:checked')].map((n) => n.value).concat(list(data.selected_ids)));
  if (!selected_ids.length) throw new Error("请明确选择至少一条记录。");
  exported = await api("bridge.export", { selected_ids, mode: data.mode, allow_sensitive_fields: [] });
  showJSON("export-preview", exported); $("export-download").hidden = false; status("导出预览已生成。请检查原文后，再确认下载；尚未上传到其他设备。");
}); });
$("revocation-form").addEventListener("submit", (event) => { event.preventDefault(); run(async () => {
  const selected_ids = [...document.querySelectorAll('input[name="revoke-record"]:checked')].map((n) => n.value);
  if (!selected_ids.length) throw new Error("请明确选择至少一条撤销记录。");
  exported = await api("bridge.export_revocations", { selected_ids });
  showJSON("export-preview", exported); $("export-download").hidden = false; status("撤销包预览已生成。接收端确认后将删除相应的已导入副本，请核对来源。 ");
}); });
$("revocation-form").addEventListener("input", () => { exported = null; $("export-download").hidden = true; $("export-preview").hidden = true; });
$("export-download").addEventListener("click", () => {
  if (!exported) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(exported, null, 2)], { type: "application/json" }));
  const anchor = element("a"); anchor.href = url; anchor.download = "tmcra-memory-package.json"; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  status("JSON 已下载。文件可能含选定记录原文，请只交给预期的目标设备。");
});
$("import-file").addEventListener("change", async () => {
  imported = null; clearImportPreview(); $("import-items").replaceChildren(); $("import-result").hidden = true;
  const file = $("import-file").files[0]; if (!file) return;
  try {
    if (file.size > MAX_PACKAGE_BYTES) throw new Error("记忆包超过 400 KB，请拆分成更小的显式选择。");
    const value = JSON.parse(await file.text());
    const items = value.items || value.records;
    if (!value || typeof value !== "object" || !Array.isArray(items) || !items.length || items.length > 100) throw new Error("需要包含 1–100 条记录的记忆包 JSON。");
    imported = value;
    for (const item of items) {
      if (!item || typeof item !== "object" || typeof (item.item_id || item.id) !== "string") throw new Error("记忆包中的记录 ID 无效。");
      const choice = sourceCheckbox(item, "import-record"); choice.querySelector("input").addEventListener("change", () => { clearImportPreview(); }); $("import-items").append(choice);
    }
    $("import-file-status").textContent = `已读取 ${items.length} 条候选；尚未导入。请勾选要迁移的条目。`;
  } catch (error) { imported = null; $("import-items").replaceChildren(); $("import-file-status").textContent = error.message; }
  updateImportButtons();
});
$("import-preview-button").addEventListener("click", () => run(async () => {
  const selected_ids = [...document.querySelectorAll('input[name="import-record"]:checked')].map((n) => n.value);
  if (!imported || !selected_ids.length) throw new Error("请先选择导入条目。");
  preview = await api("bridge.preview", { package: imported, selected_ids, allow_sensitive_fields: [] });
  showJSON("import-preview", preview);
  if (preview.revocations?.length) {
    const targets = preview.revocations.flatMap((item) => item.targets || []).map((item) => item.local_message_id || item.local_id);
    $("import-warning").textContent = `此包包含 ${preview.revocations.length} 项撤销，会删除或撤销目标端先前导入记录：${targets.join("、") || "当前无已映射记录"}。请逐项核对下方预览后再确认。`;
    $("import-warning").hidden = false;
  }
  $("import-confirm-label").hidden = false; $("import-commit").hidden = false; $("import-confirm").checked = false;
  status("服务端预览已返回。请核对冲突、来源与目标，再进行第二次确认。");
}));
$("import-confirm").addEventListener("change", updateImportButtons);
$("import-commit").addEventListener("click", () => run(async () => {
  if (!preview?.preview_token || preview.can_commit === false || preview.blocked?.length || !$("import-confirm").checked) throw new Error("请先预览并确认。");
  const result = await api("bridge.commit", { preview_token: preview.preview_token });
  showJSON("import-result", result); clearImportPreview(); await refresh(); status(`导入状态：${result.status || "已接收"}。请查看逐项结果；${indexDescription(result)}。`);
}));
if (personal) {
  $("local-connection").hidden = true; $("project").required = false; $("token").required = false; $("personal-back").hidden = false;
  $("personal-project-label").hidden = false;
  $("connection-note").textContent = "项目来自当前账号的服务端目录，与插件使用同一项目记忆。默认全局记忆；身份和实际 Scope 由服务端核对，页面不接收控制密钥。";
  connected = true; run(refresh);
}
window.addEventListener("pagehide", () => { token = ""; });
