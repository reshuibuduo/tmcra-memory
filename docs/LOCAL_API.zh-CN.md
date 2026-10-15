# 本地 API 合同

Base URL：`http://127.0.0.1:2009`

除 `/v1/health` 外，所有接口都需要 `.tmcra/config/runtime/secrets/local-api.token` 中的 Bearer Token。服务只接受本机回环请求。

## 一轮对话的正确顺序

1. 用户在 Agent 工具中提交当前问题。
2. 接入层用当前 `project_id` 和当前问题调用 `POST /v1/recall`。
3. 接入层把 `prompt_evidence.content` 作为不可信记忆证据注入，再让模型生成回答。
4. 模型回答完成后，分别调用两次 `POST /v1/messages`，写入用户消息和 Agent 回答。

当前问题尚未出现时无法判断需要召回什么。用户与 Agent 的内容不能合并成一条消息。

## 召回

```http
POST /v1/recall
Authorization: Bearer $TMCRA_LOCAL_TOKEN
Content-Type: application/json

{
  "project_id": "project-stable-id",
  "query": "我们上次决定采用什么重试策略？",
  "top_k": 8
}
```

返回内容包括：

- `resolved_scopes`：顺序固定为用户全局作用域、当前项目作用域。
- `hits`：带稳定记忆 ID 的排序结果。
- `evidence_windows`：来源原文、说话角色、会话、时间、分数和有来源约束的派生上下文。
- `prompt_evidence`：有长度边界、可直接注入的文本证据包。
- `trace`：每个作用域的真实检索摘要。它是结果完成后的记录，不伪装成实时事件流。

## 写入一条来源消息

```http
POST /v1/messages
Authorization: Bearer $TMCRA_LOCAL_TOKEN
Content-Type: application/json

{
  "project_id": "project-stable-id",
  "project_title": "示例项目",
  "session_id": "session-stable-id",
  "session_title": "功能实现",
  "role": "user",
  "content": "重试采用指数退避，最多五次。",
  "source_app": "codex",
  "native_thread_id": "native-thread-id",
  "native_message_id": "native-message-id",
  "visibility": "both"
}
```

`role` 可取 `user`、`assistant`、`system` 或 `tool`；`visibility` 可取 `project`、`global` 或 `both`。一个 Session ID 永久属于一个项目。相同原生消息身份与相同内容重复提交是幂等操作；换成不同内容会被拒绝。

## 查看与删除

```http
GET /v1/messages?project_id=project-stable-id&limit=200
DELETE /v1/messages/{message_id}
DELETE /v1/projects/{project_id}
```

删除消息会从它写入的每个作用域中移除 Source 记录及直接派生记忆。删除项目还会移除项目元数据、Personal Knowledge、模型调用用量元数据，以及该项目写入用户全局作用域的派生记录。两种操作都会压缩 SQLite。

外部备份、文件系统快照或用户 BYOK 供应商保留的副本不在本机删除范围内。

## 图谱、知识库与用量

```http
GET  /v1/projects/{project_id}/graph
POST /v1/projects/{project_id}/knowledge/build
GET  /v1/projects/{project_id}/knowledge
GET  /v1/usage?project_id=project-stable-id&limit=50
```

用量账本只记录模型、供应商、token 计数和延迟，不复制请求与回答正文。本地版 `tmcra_charge` 为零；模型供应商费用由用户与自己选择的供应商直接结算。


## 删除后的重放保护

删除消息或项目时，在同一个 SQLite 事务中保留稳定身份的哈希和撤销时间，不保留正文或原始 native ID。检查本地 message ID 与既有 `(source_app, native_thread_id, native_message_id)` 唯一键；延迟到达的 hook 上传即使只更换本地 ID、项目或 API session，也会被拒绝，不会重新创建已删除的来源。

这是身份撤销，不是正文黑名单；全新身份不因正文相同而被拒绝。重启时补记仍保留的软删除身份；升级前已物理删除且无记录的身份不会恢复。撤销记录不随项目删除而消失。源码回退不删除该表，但旧代码不执行新增保护，实际降级应评估差异。外部备份删除和多进程写入仍需独立验收。
