# 本地部署

TMCRA Local 由用户自己控制。API、数据库、Embedding 索引、公开图打分权重、用量账本和个人知识文档都保存在本机。正常运行时唯一可选的外部请求，是用户明确配置的 OpenAI 兼容生成接口。

## 环境要求

### 本机 HTTP 请求维护

Python 生成调用、生成预检和本地 API smoke 客户端对 HTTP(S) loopback 地址
显式绕过环境及操作系统代理，并拒绝重定向，包括跳到另一本机端口。
共用 Agent hooks 同样拒绝重定向。本机反向代理返回 3xx 时，应配置最终端点。
显式配置的远程 BYOK 接口保留原有代理行为，不修改全局网络设置。

回归测试使用合成数据与临时本机 HTTP 服务，不代表真实模型或设备验收。
本次维护没有安装端侧视觉/音频模型，也没有改变记忆数据格式。

### 所需工具

- Windows 10/11、主流 Linux 或 macOS。
- 必须使用 Python 3.12。
- Git 与 Git LFS。
- 默认 BYOK 档位至少需要 8 GiB 内存，建议 16 GiB。
- PyTorch、图权重与所选 Embedding 模型需要数 GiB 磁盘空间。

遇到 Git LFS 指针、模型清单不匹配、API Key 缺失或真实模型探针失败时，安装器会直接停止，不会带着残缺配置启动。

## BYOK 安装

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-local.ps1
```

Linux/macOS：

```bash
bash scripts/install-local.sh
```

模型服务需要提供 OpenAI 兼容的 `POST /v1/chat/completions`，并支持 JSON Object 响应。正常项目记忆建议使用至少 32K 上下文的模型。除非用户明确跳过，安装时会产生一次很小的 JSON 测试调用。

Windows 无人值守示例：

```powershell
$env:MY_PROVIDER_KEY = '<仅在当前进程设置>'
powershell -ExecutionPolicy Bypass -File .\scripts\install-local.ps1 `
  -NonInteractive -Mode byok -Provider openai-compatible `
  -BaseUrl https://provider.example/v1 -Model your-model-id `
  -ApiKeyEnv MY_PROVIDER_KEY
Remove-Item Env:MY_PROVIDER_KEY
```

Shell 无人值守示例：

```bash
TMCRA_BYOK_BASE_URL=https://provider.example/v1 \
TMCRA_BYOK_MODEL=your-model-id \
TMCRA_BYOK_API_KEY="$MY_PROVIDER_KEY" \
bash scripts/install-local.sh
```

运行配置只记录密钥文件路径。Key 本身写入 `.tmcra/config/runtime/secrets/byok-api.key`。安装器会把仓库内的 `.tmcra/` 目录限制为当前用户专用：POSIX 使用 `0700`，Windows 移除继承权限并只授予当前用户；运行时还会重新收紧配置、凭据和状态文件。解析后的供应商 Key 只保留在进程内模型客户端上，不写入可被子进程继承的环境变量。

## 模型与设备档位

以下命令只查看策略，不会下载：

```bash
.tmcra/venv/bin/python -m tmcra_local models
.tmcra/venv/bin/python -m tmcra_local recommend --ram-gib 16 --vram-gib 0 --language multilingual
```

稳定 Embedding 档位：

| 档位 | 用途 |
| --- | --- |
| `compact-zh` | 内存较小、中文为主的机器 |
| `balanced-multilingual` | 默认多语言档位 |
| `enhanced-multilingual` | 内存更充裕的多语言档位 |

默认 Reranker 为 `local-dense-only`。其他精排模型只有在下载和真实推理合同满足后才开放；模型卡成绩不会被写成 TMCRA 系统成绩。

Windows 用 `-Torch cpu`、`-Torch cu128` 或 `-Torch skip` 选择 PyTorch；Shell 使用 `TMCRA_TORCH_CHANNEL=cpu|cu128|skip`。`auto` 只在检测到 `nvidia-smi` 时选择 CUDA。

## 完全本地生成

本地生成需要兼容的 `llama-server`。TMCRA 会在 `127.0.0.1:2010` 启动它，通过绝对路径加载模型，以密钥文件传递本地认证，等待健康检查，并且只关闭自己启动的进程。

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-local.ps1 `
  -Mode local-model `
  -GenerationRuntimeExecutable C:\path\to\llama-server.exe `
  -AcceptLargeModel
```

Shell：

```bash
TMCRA_INSTALL_MODE=local-model \
TMCRA_LLAMA_SERVER=/path/to/llama-server \
TMCRA_ACCEPT_LARGE_MODEL=1 \
bash scripts/install-local.sh
```

推荐的完整质量本地档位使用 32K 上下文，下载约 12.74 GiB。配置较小的机器应优先选择 BYOK。

## 核验与启动

```bash
.tmcra/venv/bin/python -m tmcra_local doctor \
  --config .tmcra/config/runtime/local-runtime.json \
  --probe-models --probe-generation

bash scripts/start-local.sh
```

Windows 对应使用 `.tmcra\venv\Scripts\python.exe` 和 `scripts\start-local.ps1`。

启动后可访问：

- `GET http://127.0.0.1:2009/v1/health`
- OpenAPI 页面 `http://127.0.0.1:2009/docs`

稳定本地版没有绑定公网地址的选项。

API 启动后，运行一次会创建临时项目并清理数据的全链烟测：

```bash
.tmcra/venv/bin/python scripts/smoke_local_api.py
```

Windows：

```powershell
.\.tmcra\venv\Scripts\python.exe .\scripts\smoke_local_api.py
```

脚本会核验本地认证、用户与 Agent 分开写入、全局与项目双 Scope 召回、
来源角色、Visual Atlas、个人知识整理、本地用量账本、单条记忆删除和项目删除。
它使用唯一临时项目，并在 `finally` 中清理。Writer 与已启用的个人知识整理
可能消耗用户自己的模型接口 Token；召回只使用本地检索，不调用供应商模型。
本地 bearer 从密钥文件读取，既不会打印，也不能作为命令行参数传入。
默认烟测要求个人知识由已配置模型生成并附带可追溯证据。若主动关闭了该可选
任务，可以添加 `--allow-knowledge-fallback`；发布验收不得使用这个参数。

烟测通过后，按[本地工具接入](LOCAL_INTEGRATIONS.zh-CN.md)连接宿主。Codex 已提供一条命令安装；DeepSeek Harness 是经过生命周期测试的技术预览；Claude Code 和 ZCode 当前提供手动 Hook 清单，尚未宣称一键安装。

## 数据与备份

仓库内的 `.tmcra/` 保存虚拟环境、模型、配置、凭据和 SQLite 数据，并已被 Git 忽略。做文件级备份前应先停止 TMCRA，确保数据库与 WAL 状态一致。

TMCRA 的删除操作会压缩当前 SQLite 数据库；外部备份、快照以及模型供应商已经保留的数据需要单独处理。

## 卸载

默认卸载只删除可重新安装的 Python 环境，保留记忆、模型、配置和凭据：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall-local.ps1
```

```bash
bash scripts/uninstall-local.sh
```

需要清空本仓库克隆下的全部 TMCRA 本地数据时，Windows 使用 `-PurgeData`，Shell 使用 `--purge-data`。两个脚本都会先验证删除目标。
