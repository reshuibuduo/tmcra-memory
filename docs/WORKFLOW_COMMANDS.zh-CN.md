# 四功能怎么调用

推荐入口：**Skill 理解意图 → `tmcra_workflows` MCP → 现有 HTTP API**。四个功能不需要四套工具。Skill 可发现、MCP 启动、接口接受和内容已索引是不同状态。

| 你要做什么 | 对 Agent 说 | 实际调用 |
| --- | --- | --- |
| 接续项目 | `$tmcra-workflows resume`，检查当前项目的下一步与旧失败 | `tmcra_workflows {session_id,project_path,operation:"resume"}` |
| 看决策变化 | `$tmcra-workflows timeline`，比较方案为什么改变 | `operation:"timeline"`，需要时加 `as_of` |
| 查看提醒 | `$tmcra-workflows remind` | `operation:"reminders"` 只读；真实提问/切换时用 `execute` + `reminder.evaluate` |
| 选择性迁移 | `$tmcra-workflows bridge`，只预览我选择的来源 | snapshot 列来源 → export 确认 → 目标 preview → 第二次确认 commit |

`session_id/project_path` 来自当前宿主。MCP `execute` 必须有 `action/payload/operation_id`，插件/local 版本没有可任意指定的 tenant/scope 字段。完整平台 `08-tmcra-mcp-server` 通用服务另用授权 `scope`，不使用 `project_path`；不要把两种适配器参数混用。相同重试使用同一 operation ID；已接受不等于已索引，导入仍 `external_unverified`，普通“记住了/测试通过”不构成机器验证。记忆包撤销、导出、提交、可能写入的计划恢复、失败任务重试、删除均保留具体 host 确认；用户取消或宿主不支持确认时不绕去 CLI/API。

[实际 MCP / HTTP 四功能请求与错误处理](WORKFLOW_MCP_API.zh-CN.md) · [证据、回执与完整边界](WORKFLOW_CENTER.zh-CN.md)

## 本地版 `tmcra-memory`

先启动已有本地 API，再执行仓库原安装脚本 `scripts/install-codex-local.sh` 或 `scripts/install-codex-local.ps1`；本次新增 MCP 与 Skill 路径随完整插件目录一起分发。安装器记录已安装 runtime 的 Python 绝对路径，只存 token 文件引用。Codex 重载插件后可选 `$tmcra-workflows`；是否成功加载以客户端工具列表中的 **tmcra_workflows** 和一次真实 `resume` 返回为准。

真实本地 CLI（用安装环境的 `tmcra-local`，不是系统里的旧版本）：

```sh
tmcra-local workflow --project-path /actual/project resume
tmcra-local workflow --project-path /actual/project timeline
tmcra-local workflow --project-path /actual/project reminders
tmcra-local workflow --project-path /actual/project request --input request.json --confirm
```

本地 CLI 的所有 `request` 均要求 `--confirm`；敏感动作还要求真实 TTY 具体确认，非交互调用不放行。

MCP stdio 实际入口：`python -m tmcra_local.workflow_mcp`（或安装后的 `tmcra-workflow-mcp`），与完整平台同名同 schema。需要显式注册的 MCP 客户端可使用已安装 runtime Python 的绝对路径，args 为 `["-m","tmcra_local.workflow_mcp"]`；默认读取 `~/.tmcra/local-integration.json`，可用 `TMCRA_LOCAL_INTEGRATION_CONFIG` 选择已配置文件，不在 MCP JSON 填 token。Claude Code 可手动注册该 stdio MCP；本仓暂不宣称自动安装 Claude 斜杠 Skill 或验证最新版 Claude hooks 包装。可在 Claude 对话明确要求调用 `tmcra_workflows`。

本地 HTTP 为 `/v1/projects/{project_id}/workflows`，loopback + owner token。项目身份沿用本地 hooks 的 marker/Git/path 规则；与完整平台 tenant scope 不互通。MCP 只调用现有 API，不另起模型或上传到完整平台。跨端迁移仅显式文件包，不是后台同步。

若已有集成配置未记录新 Python 路径，重跑集成安装器或设置 `TMCRA_LOCAL_PYTHON` 为安装 runtime 的绝对路径；缺少模块时先更新安装的 runtime。不要只复制 Skill 后宣称 API 可用。
