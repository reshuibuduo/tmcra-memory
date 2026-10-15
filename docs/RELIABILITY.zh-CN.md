# 记忆可靠性：来源、后台写入与稳定实体

这批改动解决三项基础问题，不新增一套平行的 Skill/MCP 工作流。
现有任务恢复、决策历史、情境提醒和选择性记忆包仍从原入口调用。

## 1. 来源和消息角色分别保存

`role=user` 是对话传输角色，不是人类作者认证。新写入同时记录：

- `origin_kind`：`human_authored`、`human_adopted`、`app_generated`、`automation`、`assistant_generated`、`tool_generated` 或 `unknown`。
- `capture_method`：宿主事件声明、显式 API 声明或旧版仅角色记录。
- `provenance_strength`：`host_declared`、`caller_declared` 或 `unknown`。这些都是声明，不是身份认证。

自动生成提示、定时事件、代理消息不得仅因被宿主包装为 user 而成为用户偏好。
来源缺失的旧消息保持 unknown；其原文、内容哈希与历史不被批量改写。
正文中的引用或“这不是用户写的”字样不作为自动删文规则。
派生内容保留来源约束，检索结果中的证据仍是数据，不构成指令。

手工 API 调用若要声明自己的陈述，可以在 `actor` 中显式传入：

```json
{"origin_kind":"human_authored","capture_method":"explicit_api_declaration","provenance_strength":"caller_declared"}
```

不要给来源不明的自动内容补这个标记。未声明来源的内容可以保留原文，但不自动提炼为人类偏好。
**兼容性变化：** 尚未提供来源字段的宿主仍可保存和检索 Source 原文，但不会自动生成该 user 消息的语义断言；不要用角色或正文猜测来绕过此规则。

## 2. 接收原文与语义建索引分开

本地新增可选异步写入接口；原同步 `POST /v1/messages` 保留。
异步请求先将原文和任务持久化，再由单个后台 worker 加工。
`accepted=true` 表示原文和任务已经接收，`indexed=true` 表示 Source 图索引完成；两者都不能单独证明生成了语义记忆。另看 `derived_indexed` 与 `semantic_processing`。来源不明时返回 `skipped_origin_unverified`，保持零语义模型调用。

长模型调用不应持有读路径使用的全局图锁；读者读取已提交快照，写者仍按顺序提交。
删除先记录撤销标记，提交前再次复核，避免排队中的消息把已删除内容写回来。
进程中断留下的未知结果单独标记，不自动重复可能已执行的调用。
这是单 worker 的有序队列，不是跨进程并行写入或跨请求微批处理。

Hook 配置的 `ingestionMode` 可设 `async` 或 `sync`：新生成配置默认 `async`，旧配置缺字段保留同步行为。需要在原集成配置中显式切换，不修改 token 或项目身份。异步遇旧服务 404 会保留未接收项并标记版本不支持，不悄悄切回等待语义模型的同步接口。

异步 API：

| 调用 | 结果 |
| --- | --- |
| `POST /v1/message-jobs` | HTTP 202；返回 `job_id` 和接收状态 |
| `GET /v1/message-jobs/{job_id}` | 查看排队、执行、索引、失败或取消状态 |
| `POST /v1/message-jobs/{job_id}/retry`，`{"confirmed":true}` | 明确重试，最多三次执行尝试；已撤销数据不再入队 |

这些接口沿用现有 loopback Bearer token，不产生新的账号或费用。
任务会区分暂时失败、永久失败和进程中断后的未知结果；没有自动重试未知调用。

### 本地运行模式

- `off`：本进程不启动本地 API/生成模型。停止采集还需要停用插件/Hook；独立运行的 Hook 仍可能写本地 outbox。
- `read-only`：只读取已有记忆和工作流，不初始化生成客户端，不接受写入或后台加工。
- `enrich`：允许写入与后台语义加工，继续使用既有模型配置。

使用既有配置文件启动时可选择（只影响这次调用）：

```bash
tmcra-local start --config /absolute/path/local-runtime.json --mode read-only
```

将 `read-only` 改成 `off` 或 `enrich` 可选择其他模式。`off` 是拒绝本次启动，不是停止另一个已经运行的进程；停止既有服务仍用它的原管理器。
旧配置省略 `runtime_mode` 时维持 `enrich` 兼容性，新模式不偷偷改变原配置。只读使用现有图与词法路径，不加载生成模型或 embedding 模型；检索质量不等同完整模型检索。

仓库代码的模式支持不等于修改你电脑上的启动设置。安装、开机启动、插件开关均需独立检查；本次开发不会自动启动它们。

## 3. 稳定实体与更正历史

稳定实体 ID 用于说明“这一条仍是在说同一个对象”；名称、简称和中英文名是显式别名。
同名、拼写接近或向量相似不是自动合并依据。别名只在当前 scope 内参与候选检索；歧义保留，跨项目不合并。
候选增强复用既有 reconciliation，不能把“候选找到了”当成“模型更正一定正确”。

更正应该保留旧事实、当前事实、来源和生效时间。重放同一事件应幂等；查询历史仍可解释当时为什么得出旧结论。
确定性测试可以验证候选覆盖和历史关系，但不代表真实模型在所有中英文对话上都达到同等准确率。

本地消息的 `identity_metadata` 示例（完整平台放在消息 `metadata.identity_metadata`）：

```json
{
  "entity_bindings": [{"entity_id":"device:phone-01","entity_keys":["user.phone","user.handset"],"aliases":["我的手机","work phone"]}],
  "corrections": [{"entity_id":"device:phone-01","attribute_key":"location","effective_at":"2026-10-16T09:30:00+00:00","reason":"用户明确更正"}]
}
```

声明只绑定明确列出的 `entity_keys`，按既有提炼器的键格式统一分隔符和保留前缀；中英文名称放在 `aliases`，不触发自动翻译或模糊合并。提炼请求只收到这些候选标签，不把声明当作事实证据。
同一 `message_id` 重放时声明保持不可变；修正声明应成为新的消息，不改旧消息。

## 验证边界

测试使用临时库与确定性/故障注入模型，覆盖来源污染、慢写期间读取、重启恢复、重放、删除以及同名负例。
这些测试不启动已安装的个人记忆服务，不访问个人记忆库，也不验证 8B 模型的真实提炼质量。
模型效果、生产资源占用和宿主实际传入的来源字段需在后续明确启用后的验收中单独测量。
