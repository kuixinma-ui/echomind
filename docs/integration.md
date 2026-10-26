# 真实联调：先存储，再观测

推荐本机 Docker Compose。Redis/Chroma 的连接、TTL、持久化和隔离需要真实服务验证；Langfuse 再单独验收 trace/score 入库与读回。分阶段启动能缩小故障范围，所有测试数据留在独立项目和数据卷中。[Langfuse 官方 Compose 指南](https://langfuse.com/self-hosting/deployment/docker-compose)也将 Compose 定位为本地试用入口。

当前本机已经完成Docker/WSL准备，8个独立服务healthy，真实存储/默认embedding/Langfuse读回及3条端到端smoke通过。证据与范围见[真实联调结果](superpowers/plans/2026-10-26-live-integration-results.md)。以下命令用于复现环境；本指南不自动接受协议或重启系统。

项目内另有 Python 3.11 的 `.venv-integration`，已使用对应的 Windows 预编译包安装固定 Chroma/Langfuse 依赖；它不替代原 Python 3.12 `.venv`，也不代表 Redis、Chroma HTTP 或 Langfuse 平台已启动。

已临时启动并停止原生Chroma HTTP服务，完成一次显式向量的基础回环；这不等于Compose部署验收。完整结果与额外脚本的状态码断言问题见 [补测记录](superpowers/plans/2026-10-26-chroma-http-check.md)。Redis和Langfuse平台仍待服务就绪。

## 1. 文件与隔离范围

| 文件/服务 | 用途 |
| --- | --- |
| `backend/compose.integration.yml` | 独立项目 `echomind-integration`，不会复用原部署的容器名或数据卷 |
| `backend/compose.langfuse-headless.yml` | 可选本地测试组织/项目/key初始化；不创建外部账户或UI用户 |
| `backend/.env.integration.example` | 无真实凭据的配置模板；生成的 `.env.integration` 已被 `.gitignore` 排除 |
| `backend/Dockerfile.observability` | 可选一次性 CLI 镜像，安装主依赖及 `requirements-observability.txt`；原生产 Dockerfile 保持原样 |
| `redis` / `chromadb` | 默认启动，宿主机端口分别为 `127.0.0.1:16379` / `127.0.0.1:18001` |
| `langfuse` profile | web、worker、Postgres、ClickHouse、MinIO、专用 Redis 六组件 |
| `app` profile | 一次性评测 CLI；默认运行无模型调用的预检 |

Langfuse 使用自己的 Redis。评测 Redis 清理、TTL 测试或服务重启不会清空 Langfuse 队列。只有 Langfuse UI (`13000`) 和 MinIO API (`19090`) 额外发布到 `127.0.0.1`；Postgres、ClickHouse、Langfuse Redis、worker 均不发布宿主机端口。数据卷名称由独立 Compose project 加前缀。

Langfuse 配置参考已拉取的官方仓库提交 `0e024ca8eae1da68f08514a46edb7a37a4f2d0b2`（v4.70.0）的 [Compose 文件](https://github.com/langfuse/langfuse/blob/v4.70.0/docker-compose.yml)。2026-10-26 已使用公开 registry 的匿名 manifest API 确认以下镜像存在；默认值均固定到返回的 manifest digest：

| 镜像 | 固定版本/来源 |
| --- | --- |
| Redis | `7.4.2-alpine` |
| Chroma | `0.5.23`，与 Python 客户端一致 |
| Postgres | `17.6-alpine` |
| ClickHouse | `25.12`，来自上述官方 Compose 的版本系列 |
| Langfuse web / worker | `4.70.0` |
| MinIO | 官方 Compose 的 `cgr.dev/chainguard/minio`，固定到当日解析的 digest |

镜像 manifest 可读只证明版本存在，不能证明已完成拉取、启动、迁移或平台验收。可在 `.env.integration` 通过 `INTEGRATION_*_IMAGE` 明确覆盖镜像；变更后需重新记录实际镜像 digest。应用镜像的 Python 基础镜像与 pip 间接依赖仍需在正式发布时进一步锁定。

## 2. 本地生成配置

以下 PowerShell 命令从项目发布目录执行。生成过程只写入被忽略的文件，不显示密码，也不会覆盖已有配置：

```powershell
Set-Location D:\project\echomind\github-release\EchoMind\backend
@'
from pathlib import Path
import secrets

template = Path('.env.integration.example').read_text(encoding='utf-8')
secret_names = {
    'INTEGRATION_REDIS_PASSWORD', 'INTEGRATION_POSTGRES_PASSWORD',
    'INTEGRATION_CLICKHOUSE_PASSWORD', 'INTEGRATION_LANGFUSE_REDIS_PASSWORD',
    'INTEGRATION_MINIO_PASSWORD', 'INTEGRATION_LANGFUSE_SALT',
    'INTEGRATION_LANGFUSE_ENCRYPTION_KEY', 'INTEGRATION_LANGFUSE_NEXTAUTH_SECRET',
    'ECHOMIND_TELEMETRY_HASH_SALT',
}
generated = {name: secrets.token_hex(32) for name in secret_names}
lines = []
for line in template.splitlines():
    name, separator, value = line.partition('=')
    if name in generated:
        line = name + '=' + generated[name]
    elif name == 'REDIS_URL':
        line = 'REDIS_URL=redis://:' + generated['INTEGRATION_REDIS_PASSWORD'] + '@127.0.0.1:16379/0'
    lines.append(line)
with Path('.env.integration').open('x', encoding='utf-8', newline='\n') as target:
    target.write('\n'.join(lines) + '\n')
print('Created ignored .env.integration; secrets were not printed.')
'@ | python -
```

随后仅在该文件中配置实际模型的 `ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL`、固定的 `ECHOMIND_JUDGE_MODEL`。DeepSeek 的 Anthropic 兼容接口需要 `https://api.deepseek.com/anthropic`，具体模型应以账户实际支持为准。[DeepSeek 官方说明](https://api-docs.deepseek.com/guides/anthropic_api/)

如果只做存储验证，可以暂时留空模型凭据。生成器将同一 Redis 密码写入显式的 `REDIS_URL`，无需预检工具执行变量展开。修改端口时，同时修改宿主机的 `REDIS_URL`、`CHROMA_PORT`、`LANGFUSE_BASE_URL`；Compose 内部服务仍使用自己的端口。

## 3. 启动并验收存储

```powershell
docker version
docker compose version
docker compose --env-file .env.integration -f compose.integration.yml config --quiet
docker compose --env-file .env.integration -f compose.integration.yml up -d --wait redis chromadb
docker compose --env-file .env.integration -f compose.integration.yml ps
```

使用 `config --quiet`，避免把展开后的凭据打印到终端记录。Compose 会读取未启用 profile 的变量，因此上一步会一次生成全部本地服务密钥；没有启动 Langfuse。

宿主机已有项目 Python 环境时：

```powershell
# 选用已安装主依赖的环境；本机兼容性验证使用 .venv-integration。
$py = (Resolve-Path ..\.venv-integration\Scripts\python.exe).Path
$env:NO_PROXY=($env:NO_PROXY+',127.0.0.1,localhost,::1,redis,chromadb,langfuse-web').Trim(',')
& $py -m evaluation.integration --env-file .env.integration
& $py -m evaluation.integration --env-file .env.integration --stage storage
```

默认命令只检查环境，不调用模型、不读写存储；exit code `2` 表示完整实时验收尚未进行/存在 blocked 项。`--stage storage` 明确连接 Redis 与 Chroma，使用独立评测命名空间，写入合成内容并清理。必须查看生成的结构化报告，分别确认服务身份、读写、TTL、隔离、清理结果，不能把“客户端创建成功”视为通过。

Redis 的 INFO/version 与命令成功只证明兼容协议。报告中的 `server_identity=unverified_redis_compatible` 需要结合上面的 Compose 服务、实际镜像与 `ps` 输出确认来源，不能把 fakeredis 监听端口当成真实 Redis。Chroma 探针直接调用固定 0.5.23 HTTP 协议和显式测试向量，不下载默认 embedding，也不会转入本地库。

如果宿主机尚未装好 Chroma，可改用独立的 Linux CLI 镜像：

```powershell
docker compose --env-file .env.integration -f compose.integration.yml --profile app build app
# 不自动启动依赖，只运行无模型预检。
docker compose --env-file .env.integration -f compose.integration.yml --profile app run --rm --no-deps app
docker compose --env-file .env.integration -f compose.integration.yml --profile app run --rm app python -m evaluation.integration --stage storage --output-dir data/integration
```

`app` 的默认命令只预检，不提供 API 服务。它通过环境变量连接 `redis:6379` / `chromadb:8000`，使用同一份本地配置的 Redis 密码；宿主机报告写到 `backend/data/integration`。Chroma 默认 ONNX embedding **由客户端执行**，HTTP 模式也一样。此镜像首次真实文本检索会下载模型到独立 `embedding-cache` 卷；下载/缓存检查和确定性测试 embedding 的结果应分开记录。客户端安装/版本检查通过不代表模型可用。

## 4. 单请求模型检查与小样本

先显式调用最多一次、输出上限 64 token 的模型协议检查：

```powershell
& $py -m evaluation.integration --env-file .env.integration --stage provider
# 或：
docker compose --env-file .env.integration -f compose.integration.yml --profile app run --rm --no-deps app python -m evaluation.integration --stage provider --output-dir data/integration
```

模型检查通过、存储检查通过后，再显式进行最多 3 条场景的真实回放。模型和 Judge 都会调用收费接口；3 条场景不等于 3 次模型请求，意图识别、工具循环、记忆和 Judge 可能增加请求数。

支持thinking禁用的DeepSeek兼容端点可在本地配置明确设置 `ECHOMIND_STRUCTURED_THINKING=disabled`；其他提供商保持 `provider_default`。该选项仅用于JSON结构化调用，进入Judge/retrieval指纹，不增大原Token上限。默认thinking曾使256 Token的Judge预算耗尽，结果按invalid保留，详见真实联调结果。

```powershell
# 显式加载联调文件，覆盖可能遗留的宿主机环境配置。
& $py -c "from dotenv import load_dotenv; load_dotenv('.env.integration', override=True); from evaluation.scenario_runner import main; raise SystemExit(main())" --mode live --limit 3 --judge
# 或在容器中使用已注入的 .env.integration：
docker compose --env-file .env.integration -f compose.integration.yml --profile app run --rm app python -m evaluation.scenario_runner --mode live --limit 3 --judge --report-dir data/eval/scenarios
```

只使用数据集里的合成用户与知识，不复用原始业务集合。先看每条实际回答、工具证据、失败状态、延迟及 usage，再决定是否扩大到全部 120 条。服务缺失、HTTP 失败后本地 fallback、Judge 无效、清理失败均不能写成完整联调通过；本阶段不自动提升 baseline。

## 5. 启动 Langfuse 并读回证据

```powershell
docker compose --env-file .env.integration -f compose.integration.yml --profile langfuse up -d --wait
docker compose --env-file .env.integration -f compose.integration.yml --profile langfuse ps
```

打开 [本地 Langfuse](http://localhost:13000)，创建本地用户、组织和项目，再把项目 API keys 填入被忽略的 `.env.integration`：

```dotenv
ECHOMIND_TELEMETRY_ENABLED=true
LANGFUSE_PUBLIC_KEY=<本地项目的Public Key>
LANGFUSE_SECRET_KEY=<本地项目的Secret Key>
LANGFUSE_BASE_URL=http://localhost:13000
ECHOMIND_TRACE_CONTENT=false
```

Langfuse 没有默认管理员账号；首次注册/项目创建由使用者在本地界面完成。宿主机安装 `backend/requirements-observability.txt` 才能导出（当前锁定 SDK `5.0.4`，Python >=3.11）；可选 CLI 镜像已安装它。只设置环境变量不会让原生产镜像自动拥有 SDK。容器中的 `LANGFUSE_BASE_URL` 已覆盖为 `http://langfuse-web:3000`。

重新运行 1–3 条合成场景，并保留生成的 run_id。成功标准是从 Langfuse UI/公开 API **实际读回同一个 run/case 的 trace 与 score**，确认 agent/tool/generation 层级、模型 usage、错误状态、内容默认关闭及脱敏效果。SDK 能 import、序列化成功、`flush()` 返回、业务回答正常、`/health` 返回 200 都只是中间证据。

现已有独立的零LLM平台探针，显式提交一个合成trace/generation/score并轮询真实API：

```powershell
& $py -m evaluation.integration --env-file .env.integration --stage langfuse
```

该探针的usage2/3是合成传输夹具，不代表真实模型用量。默认仅允许本机/Compose端点；没有双key或显式BASE_URL时blocked，不回退Cloud。Langfuse4.70默认events_only，读回使用observations v2和scores v3；旧GET API的404不能当作未落库。参数与源码依据见[平台准备文档](superpowers/plans/2026-10-26-langfuse-readback-prep.md)。

### 可选：无UI本地项目初始化

本机本轮使用了headless测试项目，只初始化本地组织、项目和key。`.env.integration` 已有匹配的 `LANGFUSE_INIT_ORG_ID`、`LANGFUSE_INIT_PROJECT_ID`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`，可以直接复用。新的独立环境须生成自己的ID/key；不得提交这些值，也不要将已有其他项目的key重新分配给新ID。

```powershell
docker compose --env-file .env.integration -f compose.integration.yml -f compose.langfuse-headless.yml --profile langfuse config --quiet
docker compose --env-file .env.integration -f compose.integration.yml -f compose.langfuse-headless.yml --profile langfuse up -d --wait
```

初始化变量、key格式与幂等边界在平台准备文档中说明。这个模式不提供UI登录用户；如需界面查看，另配置本地用户。web/worker的健康检查使用实际监听的容器HOSTNAME，Compose中以 `$$` 转义；单纯检查容器内127.0.0.1会出现假不健康。

## 6. 停止与保留证据

```powershell
docker compose --env-file .env.integration -f compose.integration.yml --profile app --profile langfuse down
```

该命令保留数据卷和报告。需要再次联调时运行对应阶段的 `up -d --wait`。不在普通停止命令中添加 `-v`，不执行全局 `docker system prune`。报告中保留本次评测命名空间及清理状态；如清理失败，先定位该命名空间，避免删除整库。

## 验收记录口径

| 层次 | 可以证明什么 | 不能替代什么 |
| --- | --- | --- |
| 离线测试/静态配置 | 参数、脱敏、断言与配置结构符合约定 | 真实依赖连接 |
| SDK/协议测试 | 固定版本 SDK 可构造、序列化和发送协议数据 | Langfuse 服务端入库/读回 |
| 真实存储检查 | 本次环境的 Redis/Chroma 命名空间读写与清理 | 全场景效果与长期性能 |
| 单模型请求 | 当前端点、凭据与模型组合可响应 | 多轮工具链与 Judge 质量 |
| 1–3 条完整链路 | 有限合成场景链路与观测证据 | 120 条全量通过及业务专家审核 |

本机已完成Compose原生解析、依赖镜像拉取/启动/健康及真实Langfuse平台验证；可选app镜像尚未构建，不能据本机Python3.11结果宣称其Linux构建通过。后续结果归入[真实联调执行计划](superpowers/plans/2026-10-26-live-integration-plan.md)。
