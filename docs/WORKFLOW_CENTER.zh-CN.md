# 记忆工作台：四个可操作闭环

入口：启动本地 API 后打开 `http://127.0.0.1:2009/workflows`。令牌只保存在当前页面内存中，不进入 URL、浏览器持久存储或导出包。选择 project 后使用四个面板；关闭页面会失去令牌。

## 1. 可验证任务接续与失败防重试

任务保存目标、下一步、版本标签、证据来源、状态和回执。网页提交的“测试通过”、Agent 的总结都只是 `reported`，不是已验证。明确在本机执行采集器才能留下 `executed_local` 回执：

```sh
python -m tmcra_local.workflow_cli \
  --db "$TMCRA_STATE/workflows/workflows.sqlite3" \
  --namespace my-project --task task-id --operation-id unique-receipt-id \
  --version commit-label --timeout 60 -- python -m pytest
```

`TMCRA_STATE` 必须是当前安装配置的状态目录，先在工作台建立同 project/task。命令按参数列表执行，不经 shell；只保留退出码、时间及 stdout/stderr SHA256，不保存输出正文或命令参数。同 operation ID 只执行一次；超时、后续失败、版本标签变化或回执过期都会撤销绿色执行状态。

**边界：**版本标签是操作者声明，并未独立证明当前工作树与标签一致。成功执行只证明这个命令退出成功，不证明测试覆盖充分、业务正确、线上部署成功或外部事实为真。

失败尝试保存配置指纹、失败摘要和再次尝试条件，不保存可能含密钥的完整配置。用同一配置触发任务情景时提醒旧失败；新条件不被永久封禁。

## 2. 决策时间机

决策保存正文、理由、来源和依赖。修订创建新版本；按 `as_of` 查看的是服务器当时已经记录的版本，而不是客户端可以回填的时间。上游决策修订后，后代标为“待复查”，不自动编造新结论。引用来源被纠正/隐藏后也要求复查。

删除是隐私擦除：删除历史正文与操作缓存并保留无正文墓碑，派生依赖一并清理。历史视图不复活删除内容。普通观点变化用“修订”，不要用“删除”。已经导出的外部文件不会被远程擦除，须明确导出撤销包并在另一端确认应用。

## 3. 情景提醒

仅在项目打开、当前提问或任务切换三个事件评估。每次最多显示三条，显示来源及“为什么此时提醒”，支持完成、忽略和延后，带冷却。没有后台定时推送，不自动执行提醒正文。

本地 Codex/Claude Code hooks 已追加独立、限时、限长的工作流上下文。老服务返回 404、网络异常都不阻塞主对话。记忆数据是非可信参考，不成为系统指令；助手声称完成不会自动成为执行回执。

## 4. 选择性跨端记忆包

这是**显式文件传递**，不是后台双向同步。操作顺序：

1. 选择“知识结论”或明确选择的原始来源，不默认导出全库。列表显示最近 50 条；API 可选择其他已知可访问来源 ID。
2. 先查看所选内容再下载。常见密钥格式触发额外字段确认；检测不是通用隐私识别器，请检查包内容。
3. 在目标 project 读取包，勾选条目并预览来源、依赖、冲突、敏感字段和撤销目标。
4. 确认后才通过正常 memory writer 入库。尚未接受的写入可用同计划再次执行；已经接受的异步任务则只查询原回执，不隐式重投。目标状态改变/令牌过期要求重新预览。
5. 删除后可选择无正文撤销身份，导出撤销包；另一端预览确认实际删除目标后执行，防止旧包换请求 ID 重新导入。

源系统实例 UUID + 原 namespace + 原 ID 在再次导出时保持；不同设备的同名项目不会误当同一来源。同身份不同内容拒绝覆盖。包 SHA256 仅检测内容完整性，**不证明发送者身份或内容真实性**；导入一律 `external_unverified`。源 role 只是出处，不授予 system/tool 权限。上限 100 条/2 MiB；不执行包内路径、URL、命令或指令。

预览令牌绑定目标 namespace、条目集合、完整包 hash、目标版本与有效期。没有附件、数据库、账号令牌或后台全量复制。两个仓库使用相同包格式。

### 已接受任务失败后的显式恢复

`accepted_pending` 仅表示后端接受任务，并非已索引。工作台查询真实任务状态：失败/取消显示失败原因类别，原始服务商错误正文不会进入工作台。只读刷新不提交任何新任务。

对于提供原生恢复适配器的完整平台，点击失败条目的“重试失败任务”并确认，或提交 `bridge.retry_failed`。仅实际 `failed` 作业接受该操作；排队、运行、成功、取消的作业均拒绝。平台沿用原 `job_id`、消息身份和 Source/journal 恢复审核，**不是重新创建一份 ingest**。审核不支持恢复时返回拒绝，需要检查原作业的恢复证据；不会绕过审核创建替代作业。本地同步 writer 没有异步已接受作业，不使用此入口。

同一 `operation_id` 只调用一次恢复适配器；尝试历史保留在条目的 `retry_history`。每个导入来源最多三次明确恢复请求，没有定时或无限重试。如果进程在外部恢复调用期间中断，保留 `retry_outcome_unknown` 并要求核对原作业，不能把请求结果未知当作未执行后再次自动发送。恢复被接受仍是待索引；只有正常任务回执及实际索引确认后才显示已完成。

## 接口与可复用请求

`GET /v1/projects/{project_id}/workflows?as_of=...`

`POST /v1/projects/{project_id}/workflows`，Bearer 认证：

```json
{"action":"task.upsert","operation_id":"project-open-001","payload":{"id":"ship","title":"发布检查","version":"commit-label","next_step":"运行回归并记录实际退出结果","evidence_ids":[]}}
```

写动作：`task.upsert`、`task.receipt`、`attempt.record`、`decision.create`、`decision.revise`、`reminder.upsert`、`reminder.evaluate`、`reminder.act`、`record.delete`。可传 `expected_revision` 防止覆盖并发修改。

包动作：`bridge.export`、`bridge.export_revocations`、`bridge.preview`、`bridge.commit`、`bridge.plan`、`bridge.retry_failed`。完整平台显式恢复请求示例：

```json
{"action":"bridge.retry_failed","operation_id":"retry-existing-job-001","payload":{"plan_id":"plan_FROM_APPROVED_IMPORT","item_id":"ITEM_FROM_PLAN","confirmed":true}}
```

`plan_id`、`item_id` 从服务端已有导入计划读取，不接受请求替换原作业、内容或来源。重试沿用当前身份与项目权限；撤销仍额外要求删除权限。`context.prepare` 供受限 hooks 组合任务证据与提醒。公开 API 不执行本机测试命令。

## 验证与回退

新增测试覆盖 SQLite、真实 HTTP、来源隔离、执行/超时、历史、依赖、提醒预算、敏感字段、预览、冲突、幂等、重试、撤销与删除重放。浏览器验收使用真实本地 API/SQLite，只有 LLM writer 边界是确定性替身；不等于真实模型记忆质量或生产多机验收。

状态只新增在 `workflows/` 下，不改变原 Source/Fast/Slow 格式。回退源代码不删除已有业务数据库；旧版本不展示新功能，也不执行新工作流删除联动。要降级运行带真实数据的服务，应先停止写入并备份，而不是删除 sidecar 来假装回退数据。
