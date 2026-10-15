# TMCRA 纯本地 Agent Hooks

这个接入包把支持生命周期 Hook 的代码工具连接到本机 TMCRA API。它不会访问 TMCRA 账户服务或 TMCRA 托管 API。

## 稳定接入

本版先完整支持 Codex 安装：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-codex-local.ps1
```

安装脚本不会把 bearer token 写进接入配置。配置只保存 `install-local` 生成的 token 文件路径。脚本会注册本地 Codex marketplace、开启 Hooks，并安装 `tmcra-local-memory`。

重启 Codex 后打开 `/hooks`，逐项审核并信任四个 TMCRA Local Memory Hook。Codex 不允许安装脚本代替用户授予 Hook 信任。

## 生命周期契约

1. `UserPromptSubmit` 根据本轮问题同时召回用户全局与当前项目记忆。
2. 召回内容放进明确的“不可信数据”边界，再注入当前上下文。
3. 用户内容先脱敏，再以独立 `user` 源记录写入；默认可见性为 `both`。
4. `Stop` 把可见回答以独立 `assistant` 记录写入当前项目。
5. 本地 API 暂时不可用时，写入进入本机 outbox，由下一次生命周期事件重试。
6. 多个工具依次使用 `.tmcra/project.json`、Git origin、Git 根目录或规范化目录路径解析同一个项目身份。

`claude-hooks.json` 与 `zcode-hooks.json` 已采用同一数据契约，可用于接入测试和手动注册。本版尚未对这两个宿主的最新公开打包流程完成独立验收，因此不宣称一键安装。

## 安全边界

- API 地址只允许 `localhost`、`127.0.0.1` 或 `::1`。
- 每次请求从文件读取本机 bearer token，接入状态不会序列化 token。
- 查询和写入前会脱敏常见凭据。
- Hook 失败时让宿主继续运行。诊断日志只记录错误类型和截断后的错误信息，不记录用户问题或回答。


## Workflow Skill and MCP

`skills/tmcra-workflows` provides resume, timeline, reminder and selective-package workflows through the actual `tmcra_workflows` MCP tool. See [MCP/API examples](docs/workflow-mcp-api.zh-CN.md). Skill files alone do not prove the client loaded the server; confirm with tools/list and a real read. Sensitive actions still require host confirmation.
