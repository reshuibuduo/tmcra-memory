# 消息来源与作者声明

消息 `role` 表示传输角色，不能证明正文由人类书写。身份鉴权仅证明请求方可调用 API，不证明每段正文的作者。

## 三个有界字段

在本地 `/v1/messages` 的 `actor`、完整服务消息的 `metadata` 中使用：

```json
{
  "origin_kind": "human_authored",
  "capture_method": "explicit_api_declaration",
  "provenance_strength": "caller_declared"
}
```

- `origin_kind`：`human_authored`、`human_adopted`、`app_generated`、`automation`、`assistant_generated`、`tool_generated`、`unknown`。
- `capture_method`：`host_event_metadata`、`explicit_api_declaration`、`legacy_role_only`。
- `provenance_strength`：`host_declared`、`caller_declared`、`unknown`。前两者都是声明，不是认证、签名或真实性保证。
- 人类来源仅允许搭配 `role=user`。未声明来源的旧请求仍可入库，但读视图为 `unknown`；旧 actor JSON/hash 不静默重写。

## 默认行为

1. Hook 只读取结构化 `origin_kind/message_origin/origin.kind/metadata.origin_kind`，以及明确的 `is_generated/is_synthetic/is_automated/automation_id`。这些是适配契约，不宣称所有宿主已提供这些字段。
2. 明确的自动或应用生成用户事件不参与自动捕获；旧 outbox 中明确标记的同类事件不再重放。正文中出现“这是生成建议”或 `<heartbeat>` 不会因此被删除：用户可能正在引用它们。
3. 宿主没有可靠来源字段时，内容保留为未知来源原始记录；不会凭 `UserPromptSubmit` 或 `role=user` 自动推断真人来源。
4. `role=user` 且来源未知、自动或应用生成的消息只保存原始 Source，不调用语义提取，不生成用户偏好/声明/任务。同一批次中有明确人类来源的其他消息正常提取。
5. 召回、Visual Atlas、知识证据保留来源；派生内容依据原始 Source，不按自身声明升级。缺失或混合来源保守显示未知。
6. Personal Knowledge 中 `personal` 页面和 `confirmed` 声明要求每个引用证据具有明确人类来源。未知/自动/助手材料可用于有归因的 `provisional` 项目或学习笔记。`confirmed` 仍不是独立事实核验。
7. 本改动不扫描或改写历史正文。历史 role-only 数据呈现为未知，来源补录需要单独可审计流程，不能用自动批量标签冒充作者认证。

## 回归入口

- `runtime/tests/test_origin_provenance.py`：来源规范化、旧 hash、派生缺失来源、原始/编译证据。
- `runtime/tests/test_source_origin_gate.py`：临时真实存储、未知/自动消息零模型调用、混合批次。
- `runtime/tests/test_origin_knowledge.py`：Personal Knowledge 门槛与 Atlas 不可变来源。
- `integrations/local-agent-hooks/tests/prompt_origin.test.mjs`：结构化分类、用户引用、旧 outbox 与零自动写入。

测试使用合成文本与临时目录，不代表实际宿主已经提供生成来源字段，也不调用真实模型。

## Hook 写入回执

新生成的 integration 配置默认 `ingestionMode: "async"`；缺失此字段的旧配置仍按 `sync` 兼容。配置脚本可显式指定 `--ingestion-mode async|sync`。异步请求走 `/v1/message-jobs`：202 仅表明接受，`indexed` 表明原始来源已提交，`derived_indexed` 才描述派生内容处理；`semantic_processing: skipped_origin_unverified` 表示因来源未知而只保留原文。receipt 不包含正文或令牌。

async 遇旧服务 404 不回落为长耗时同步请求；保留 outbox，并记录 `async_unsupported`。400/401/403/409/413/422 的终止性拒绝移出自动重试队列、保留无正文拒绝回执，不阻塞后续独立条目。已接受的任务不在 hook 中自动重投，终态失败需通过服务端明确的重试流程处理。

自动来源跳过、未知来源仅原文写入均向 stderr 发出结构化 `tmcra_capture` 状态，只有分类及固定原因，不含正文、账号或路径；stdout 仍为宿主 hook JSON。
