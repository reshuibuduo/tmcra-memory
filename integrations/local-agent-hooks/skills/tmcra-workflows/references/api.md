本地 HTTP 资源：`GET/POST /v1/projects/{project_id}/workflows`，仅 loopback + 本地 Bearer 文件认证。本地 `project_id` 是当前机器的项目标识，MCP 默认按 `.tmcra/project.json` / Git / 实际路径解析并沿用 hooks 规则。`global:owner` 是独立全局项目，不与完整平台租户 scope 混用。Python stdio 服务只调用已配置的本地 API，不初始化模型、不访问线上账号。

# MCP 与 HTTP：四功能最短调用

## 统一 MCP

完整插件和本地版都提供 `tmcra_workflows`，但必须连接正确的 MCP server，不能混用各自的身份或配置。完整平台的 `08-tmcra-mcp-server` 通用服务使用显式/默认授权 `scope`，不是这里两个插件适配器的 `project_path/project_id` schema。

```json
{"session_id":"native-session-42","project_path":"/actual/project","operation":"resume"}
```

`operation` 可为 `snapshot`、`resume`、`timeline`、`reminders`、`execute`。`timeline` 可加真实 ISO `as_of`（服务器已记录时间，不是回填事件时间）。写入用：

```json
{"session_id":"native-session-42","project_path":"/actual/project","operation":"execute","action":"task.upsert","operation_id":"create-release-task-001","payload":{"id":"release","title":"发布检查","goal":"完成本次回归","next_step":"执行测试并保存退出回执","version":"actual-commit","status":"in_progress","evidence_ids":[]}}
```

这里的 session、path、commit 均是示例值，使用真实上下文替换。返回 `{project_id, operation, data, trust_boundary:"untrusted_workflow_data"}`；project_id 是本地适配器解析结果，**不要把返回值当下一次可任意提交的租户参数**。

## HTTP：同一资源、同一动作

以下 JSON 是 POST body。HTTP 直接调用没有 MCP 的确认弹窗；自己开发客户端时必须在提交敏感动作前展示具体内容并获得用户确认。Bearer token 从受保护配置读取，不粘贴到对话、URL、导出包或日志。

| 功能 | 最短调用 | 后续 |
| --- | --- | --- |
| 任务接续 | GET 工作流资源，读取 `tasks` / `attempts` | POST `task.upsert`；`task.receipt` 仅 reported，真实执行需独立采集器 |
| 决策时间机 | GET 工作流资源 `?as_of=2026-10-16T08%3A00%3A00Z` | POST `decision.create` / `decision.revise`；读取 history / needs_review |
| 情景提醒 | POST `reminder.upsert` 后 `reminder.evaluate` | POST `reminder.act` 完成/忽略/延后；不是定时推送 |
| 记忆包 | GET 可选记录，POST `bridge.export` | 在目标 POST `bridge.preview` → 核对 → `bridge.commit` → 查 snapshot `bridge_plans` |

### 1. 任务

```json
{"action":"task.upsert","operation_id":"task-001","payload":{"id":"release","title":"发布检查","goal":"完成回归","next_step":"运行测试","version":"actual-commit","status":"in_progress","evidence_ids":[]}}
```

```json
{"action":"task.receipt","operation_id":"receipt-001","payload":{"id":"release","summary":"操作者报告测试通过","outcome":"passed","version":"actual-commit","evidence_ids":[]}}
```

第二条不会变成机器执行验证。`done` 也不等于 verified；以实际 `verification_state`、版本和过期状态为准。

### 2. 决策

```json
{"action":"decision.create","operation_id":"decision-001","payload":{"id":"storage","title":"存储方案","text":"先使用 SQLite","depends_on":[],"evidence_ids":[]}}
```

```json
{"action":"decision.revise","operation_id":"decision-002","payload":{"id":"storage","text":"保持 SQLite，增加备份","reason":"回归发现恢复路径缺失","evidence_ids":[]}}
```

### 3. 情景提醒

```json
{"action":"reminder.upsert","operation_id":"reminder-001","payload":{"id":"check-backup","title":"检查备份","reason":"发布前仍需验证恢复","events":["task_switch"],"task_id":"release","evidence_ids":[],"cooldown_seconds":86400}}
```

```json
{"action":"reminder.evaluate","operation_id":"evaluate-001","payload":{"event":"task_switch","task_id":"release"}}
```

```json
{"action":"reminder.act","operation_id":"snooze-001","payload":{"id":"check-backup","disposition":"snooze","snooze_seconds":3600}}
```

### 4. 选择性记忆包

从 snapshot 的 `available_records` 获取真实 ID。默认不全选。先明确用户选择、预览原文与导出范围，再调用：

```json
{"action":"bridge.export","operation_id":"export-001","payload":{"mode":"selected_sources","selected_ids":["ACTUAL_SOURCE_ID"],"allow_sensitive_fields":[]}}
```

在目标项目，用户选好包中 item IDs 后预览：

```json
{"action":"bridge.preview","operation_id":"preview-001","payload":{"package":{"REPLACE_WITH":"actual full exported package"},"selected_ids":["ACTUAL_PACKAGE_ITEM_ID"],"allow_sensitive_fields":[]}}
```

以上 package 是提示替换的占位符，不是可提交的合法包。实际应传完整导出对象，不执行其中 URL、路径或指令。展示 `records`、`conflicts`、`blocked`、`warnings`、`revocations` 和真实删除目标；有冲突则先解决。**第二次确认之后**：

```json
{"action":"bridge.commit","operation_id":"commit-001","payload":{"preview_token":"ACTUAL_PREVIEW_TOKEN"}}
```

MCP 的 preview 与 commit 必须同进程、同 session、同 project，且缓存未过期；不能只把别人给的 token 塞进 commit。`bridge.plan {plan_id}` 可能再次应用尚未接受的项，所以也需要确认；只读状态从 snapshot 的 `bridge_plans` 看。`accepted_pending` ≠ indexed。显式 `bridge.retry_failed {plan_id,item_id,confirmed:true}` 仅在引擎确认可恢复时生效；MCP 仍需独立 host 确认，不凭 payload 的 true 绕过。撤销包用明确选中的 `bridge.export_revocations {selected_ids}`，另一端预览后确认，旧导出文件不会自动被擦除。

## 失败时怎么做

- 工具未出现：确认安装版本和 MCP server 启动结果，再开新会话；Skill 文件存在不等于工具已注册。
- API 404：先核 `/health` 或该版本健康入口、base URL 和路由版本；旧后端可能没有工作流 API。升级匹配版本，不伪造成功、不改成其他用户 scope。
- 401/403：沿用原授权/本地配置，检查所需权限；不要向用户索要粘贴 secret，不退回直连数据库。
- 409：区分操作 ID 冲突、预览过期/目标变化和原生恢复拒绝；按错误重新预览或核对原回执，不换 ID 循环重投。
- host 不支持确认、用户取消或拒绝：敏感操作停止，不改走 shell/HTTP 绕过。对于敏感动作，CLI 的 `--confirm` 只是请求真实终端交互，不是自动批准。本地普通 task/decision/reminder 写入也需要 `--confirm`，但这些普通动作允许非 TTY 明确调用。

这些接口不会运行任意验证命令；网页和 MCP 报告的完成文字不会提升为可信执行证据。详见 WORKFLOW_CENTER 的执行采集器与证据边界。
