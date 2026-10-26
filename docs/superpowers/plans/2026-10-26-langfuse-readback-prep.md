# Langfuse 本地初始化与读回验收准备（2026-10-26）

状态：**源码准备与独立探针已实现，平台验收未执行**。最初的只读准备后，主任务另行限定授权新增探针与专项测试；没有读取任何已有 `.env` 文件、生成或输出实际密钥、启动服务、调用模型。后续执行者需要真实的本地 Langfuse 六组件服务；SDK 的导出/flush 成功不能替代服务端读回。

## 1. 固定版本与结论

- 官方参考目录：`D:\project\echomind\reference-repos\langfuse`；`web/package.json:3` 为 `4.70.0`，项目既有部署文档记录官方提交 `0e024ca8eae1da68f08514a46edb7a37a4f2d0b2`；本次 `git describe --tags --always` 返回 `0e024ca`（本地无 tag 描述）。
- EchoMind 锁定 Python SDK `langfuse==5.0.4`（`backend/requirements-observability.txt:3`）。本次还核对了项目 `.venv-integration/Lib/site-packages/langfuse` 的实际源码。
- 可以**无需 UI、无需外部账户、无需 LLM 配置**创建本地测试组织、项目及项目 API key。启动时配置 headless init 即可，用户创建是独立可选分支。未提供本地用户时，key 的创建者为 `system`。
- 当前 `backend/compose.integration.yml` **没有把 `LANGFUSE_INIT_*` 映射到 `langfuse-web`**。单纯把变量放在宿主 shell 或 Compose 的 env 文件中，不会把未声明的变量注入服务容器。需要后续执行者增加一个小的 Compose override。
- **4.70.0 默认 `LANGFUSE_MIGRATION_V4_WRITE_MODE=events_only`，且 `LANGFUSE_MIGRATION_V4_ALLOW_PREVIEW_OPT_IN=true`。** 新部署使用 `GET /api/public/v2/observations` 与 `GET /api/public/v3/scores`。旧版 trace 列表/详情、observations v1、scores v1/v2 GET 在 events_only 模式可主动返回 404，不能用这些 404 判定 SDK 没有写入。

官方源码锚点：[初始化](https://github.com/langfuse/langfuse/blob/0e024ca8eae1da68f08514a46edb7a37a4f2d0b2/web/src/initialize.ts)、[v4 默认配置](https://github.com/langfuse/langfuse/blob/0e024ca8eae1da68f08514a46edb7a37a4f2d0b2/web/src/env.mjs)、[旧 API 模式门禁](https://github.com/langfuse/langfuse/blob/0e024ca8eae1da68f08514a46edb7a37a4f2d0b2/web/src/features/public-api/server/createAuthedProjectAPIRoute.ts)。全部结论来自本地源码，没有进行网络请求。

## 2. Headless init 的精确变量及行为

总入口在 `web/src/instrumentation.ts:3-8,49-50`：Node runtime 启动时加载 `initialize.ts`；`NEXT_PUBLIC_LANGFUSE_RUN_NEXT_INIT` 未设置时默认启用，显式设为 `false` 会跳过初始化。

| 变量 | 行为/约束 |
| --- | --- |
| `LANGFUSE_INIT_ORG_ID` | **总开关**。缺少时其余 init 变量全部忽略，仅记录警告。设置后按 ID upsert 组织。 |
| `LANGFUSE_INIT_ORG_NAME` | 可选；新组织默认 `Provisioned Org`。 |
| `LANGFUSE_INIT_PROJECT_ID` | 在该组织下按 ID upsert 项目；创建项目及 key 时必需。 |
| `LANGFUSE_INIT_PROJECT_NAME` | 可选；新项目默认 `Provisioned Project`。 |
| `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` | 必须与 secret 同时非空，且已有 org/project ID，才创建项目 key。 |
| `LANGFUSE_INIT_PROJECT_SECRET_KEY` | 与 public 配对；只有 public 或只有 secret 时不会创建 key。 |
| `LANGFUSE_INIT_USER_EMAIL` | 与 password 同时非空才创建/复用本地密码用户；邮箱小写化；须是有效邮箱格式或空串。 |
| `LANGFUSE_INIT_USER_PASSWORD` | 与 email 配对；无需外部身份提供者、收信验证或外部账户。此方案不需要此分支。 |
| `LANGFUSE_INIT_USER_NAME` | 可选；新用户默认 `Provisioned User`；用户会获该组织 OWNER。 |
| `LANGFUSE_INIT_ORG_CLOUD_PLAN` | 源码支持，注释限定 CI 用途；需满足 `CloudConfigSchema`。本地验收不设置、不借此开启付费能力。 |
| `LANGFUSE_INIT_PROJECT_RETENTION` | 源码支持，整数且至少 3；仅有 `data-retention` entitlement 时生效。本地验收不设置。 |

官方 `docker-compose.yml:128-136` 透传常用的前九项，额外两项只在源码 env schema 中支持。

幂等性边界：组织和项目 upsert 的 `update: {}` 不会更名或改隶属关系；已有同一个 public key 且仍属于同一个 project 时，**不会更新 secret**。把已有 public key 指到另一个 project 会删除旧 key 再创建。使用本次专用、稳定的 org/project ID 和成对 key，避免碰到其他项目；不要把“改 secret 后重启”当作轮换方案。

### Key 形式与保管

官方 `packages/shared/src/server/auth/apiKeys.ts:86-91` 的正常生成格式为 `pk-lf-<UUIDv4>` / `sk-lf-<UUIDv4>`，两者分别生成 UUID。初始化接受预定义字符串，没有在该初始化路径校验前缀；应遵循官方格式。`SALT` 必须配置，服务端保存 secret 的哈希及显示用掩码，不能通过后续 API 恢复完整 secret。

本准备工作不生成任何 key。后续执行者应在已授权的私有配置流程中创建并保管成对凭据，不在终端、Markdown、报告、Compose 展开配置或截图中输出。EchoMind 的 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 应与 init 的同一对值对应。

### 最小 Compose override（此处仅是待执行内容）

在 `backend` 下后续新建 `compose.langfuse-headless.yml`，内容如下；不添加用户变量：

```yaml
services:
  langfuse-web:
    environment:
      NEXT_PUBLIC_LANGFUSE_RUN_NEXT_INIT: "true"
      LANGFUSE_INIT_ORG_ID: echomind-local-acceptance-org
      LANGFUSE_INIT_ORG_NAME: EchoMind Local Acceptance
      LANGFUSE_INIT_PROJECT_ID: echomind-local-acceptance-project
      LANGFUSE_INIT_PROJECT_NAME: EchoMind Local Acceptance
      LANGFUSE_INIT_PROJECT_PUBLIC_KEY: ${LANGFUSE_PUBLIC_KEY:?Supply local project public key privately}
      LANGFUSE_INIT_PROJECT_SECRET_KEY: ${LANGFUSE_SECRET_KEY:?Supply local project secret key privately}
```

之后的命令（**本次均未执行**）：

```powershell
docker compose --env-file .env.integration -f compose.integration.yml -f compose.langfuse-headless.yml --profile langfuse config --quiet
docker compose --env-file .env.integration -f compose.integration.yml -f compose.langfuse-headless.yml --profile langfuse up -d --wait
docker compose --env-file .env.integration -f compose.integration.yml -f compose.langfuse-headless.yml --profile langfuse ps
```

使用 `config --quiet`，不输出展开后的凭据。六组件为 web、worker、Postgres、ClickHouse、MinIO、Langfuse 专用 Redis；应用评测 Redis/Chroma 不能替代这些依赖。现有 profile 默认只暴露 web `127.0.0.1:13000` 和 MinIO `127.0.0.1:19090`，worker 等留在内部网络。

## 3. 健康、鉴权与读回 API

以下 `BASE` 在宿主机是 `http://127.0.0.1:13000`，在 app 容器中是 `http://langfuse-web:3000`。读回测试使用 HTTP Basic Auth：用户名 public key、密码 secret key；让 HTTP 客户端构造认证头，禁止输出凭据或认证头。

| 请求 | 期望字段/意义 |
| --- | --- |
| `GET BASE/api/public/ready` | 无鉴权；正常 200 `{"status":"OK","version":"4.70.0"}`。主要证明 web 未收到关闭信号，不检查整条 ingestion。 |
| `GET BASE/api/public/health` | 无鉴权；正常 200 同上。默认不执行数据库检查，不能证明依赖和落库。 |
| `GET BASE/api/public/health?failIfDatabaseUnavailable=true` | 额外向 Postgres 执行 `SELECT 1`；异常 503，`status` 可能是 `Database not available`。仍不证明 ClickHouse、MinIO、Redis、worker 完整链路。 |
| `GET BASE/api/public/health?failIfDatabaseUnavailable=true&failIfNoRecentEvents=true` | 可选辅助检查；events_only 时检查 `events_core` 最近 3 分钟内是否存在事件。是全局最近数据检查，不能证明本次 project/run/case。刚启动空库没有事件时失败是预期现象。 |
| worker 内部 `GET http://127.0.0.1:3030/api/health` | 从 worker 容器内部访问；正常 200 `{"status":"ok"}`，检查原生 addon、Postgres、Redis。支持 `failIfEventPropagationStuck=true`、`failIfQueueConsumptionStuck=true`，仍不替代本次读回。 |
| `GET BASE/api/public/projects` | **项目 key 鉴权检查**。200 `{"data":[{"id":...,"name":...,"organization":{"id":...,"name":...},"metadata":{...}}]}`。断言返回本次唯一项目 ID 和组织 ID；不要使用 `POST /projects` 引导初始化，它需要组织 key 及 admin-api entitlement。 |
| `POST BASE/api/public/otel/v1/traces` | SDK 默认真实 OTLP 路径。SDK 5.0.4 默认 exporter 自带 `x-langfuse-ingestion-version: 4`。此验收不要换成内存 exporter、自定义 MockTransport 或手写 legacy trace ingestion。 |
| `POST BASE/api/public/scores` | SDK `observation.score(...)` 的写入路径。数组请求全接受时为 202 `{"message":"Accepted"}`；部分拒绝可能是 207，含 `accepted/rejected/errors`。单条对象写入可返回 `{"id":...}`；均只证明接收，未证明读回。 |
| `GET BASE/api/public/v2/observations?traceId=<TRACE>&fields=basic,time,metadata,io,model,usage&limit=100` | 新 events 读路径；200 `{"data":[...],"meta":{}}`，有后页时 `meta.cursor`。项核心字段：`id`、`traceId`、`projectId`、`parentObservationId`、`type`、`startTime`、`endTime`；选定组另返回 `isRootObservation/name/level/statusMessage/metadata/completionStartTime/model/usageDetails/input/output`。 |
| `GET BASE/api/public/v3/scores?traceId=<TRACE>&observationId=<OBS>&name=readback_probe&fields=core,subject&limit=100` | 新 score 读路径；200 `{"data":[...],"meta":{"limit":100}}`，有后页时 `meta.cursor`。数值项断言 `projectId/name/dataType="NUMERIC"/value/source="API"`；**绑定关系在 `subject={"kind":"observation","id":<OBS>,"traceId":<TRACE>}`**，不是顶层 `traceId`/`observationId`。必须请求 `subject` 字段组。 |

`traceId` 从 SDK 的 `get_current_trace_id()` 在 span 内获得，为 32 位小写 hex；`get_current_observation_id()` 为 16 位小写 hex。不要用 run_id 猜 trace ID。所有 score 的 `observationId` 过滤必须同时提供 `traceId`。v3 score query 严格校验未知参数，不能沿用 v2 的 `page`、`scoreIds` 或 `fields=score,trace`。

observations v2 的 `input/output` **始终是原始字符串**（可能是 JSON 字符串或 null），`parseIoAsJson=true` 会被拒绝；需要读取结构时由验收客户端解析，不能直接比较字符串与字典。`metadata` 可防御性地兼容字典或 JSON 字符串。不要在初次查询中加 `toStartTime` 等上界而排除仍在结束中的 span。

精确源码：[v2 observations handler](https://github.com/langfuse/langfuse/blob/0e024ca8eae1da68f08514a46edb7a37a4f2d0b2/web/src/pages/api/public/v2/observations/index.ts)、[query/response schema](https://github.com/langfuse/langfuse/blob/0e024ca8eae1da68f08514a46edb7a37a4f2d0b2/web/src/features/public-api/types/observations.ts)、[v3 scores handler](https://github.com/langfuse/langfuse/blob/0e024ca8eae1da68f08514a46edb7a37a4f2d0b2/web/src/pages/api/public/v3/scores/index.ts)、[v3 score schema](https://github.com/langfuse/langfuse/blob/0e024ca8eae1da68f08514a46edb7a37a4f2d0b2/packages/shared/src/features/scores/interfaces/api/v3/schemas.ts)。

## 4. 最小不调用模型的 EchoMind 适配器验收

已实现独立模块 `backend/evaluation/langfuse_probe.py`，接口：

```python
async def probe_langfuse(config, *, timeout_s=120, transport=None,
                        clock=time.monotonic, sleep=asyncio.sleep) -> dict:
    ...
```

默认路径创建真实 Langfuse 5.0.4 SDK，经现有 `LangfuseTelemetry` / `bind_telemetry` / `get_telemetry` 写入一个 AGENT 根 observation、一个 GENERATION 子 observation 和一个 `readback_probe=0.75` 数值 score，然后 flush/shutdown。没有调用 ChatService、场景 Runner 或模型。generation 的 `model=synthetic-no-model-call`、usage 为手工合成的 `input_tokens=2/output_tokens=3`，不能当作真实模型证据。

已追踪 usage 的服务端精确转换：`OtelIngestionProcessor.ts:3030-3040` 优先读取显式 Langfuse usage 属性，再在 `550-555` 用 `UsageDetails` 解析；`packages/shared/src/server/ingestion/types.ts:215-219` 的 OpenAI Response 分支要求 `input_tokens/output_tokens/total_tokens` 同时存在。本探针只提供前两项，因此走 `RawUsageDetails`（90-113），保留原键；`worker/src/services/IngestionService/index.ts:1695-1705` 再补 `total=5`，其 `normalizeProvidedUsageDetails:1711-1725` 只转换数值、不重命名键；公开 observation converter 在 `observations_converters.ts:263-264` 保留 usage record。因此验收要求 **input_tokens=2、output_tokens=3、total=5**，不会无依据地放宽为 input/output 别名。

配置只接受调用者传入的 `config`。双 key 或显式 `LANGFUSE_BASE_URL` 缺失时返回 blocked，绝不使用 SDK 的 Cloud 默认值；缺可选 SDK 也在网络请求前返回 blocked。本地允许 localhost、127.0.0.1、::1 及 Compose 服务名 langfuse-web；其他指定测试 endpoint 须另外设置 `LANGFUSE_ALLOW_TEST_ENDPOINT=true`。URL 不接受用户密码、query、fragment 或额外路径。外部测试 endpoint 不属于本次本地验收。

成功必须实际读回同一个 trace 的根/子 observation，验证 type、project、parent、run_id/case_id、结束时间、completionStartTime、合成 model/usage、默认正文关闭、标识符 HMAC/secret 字段脱敏，以及 scores v3 的数值和 subject 关联。适配器会吞掉 exporter 异常，所以“业务完成”和“flush 返回”永远不是通过条件。trace/observation ID 从 SDK 公共方法取得。

返回值始终有 `id=langfuse`、status、evidence、`model_requests=0`、`full_integration_verified=false`、attempts；通过时另有安全的 project_id/run_id/trace_id/root_id/generation_id/score_id、布尔 checks、耗时。错误只返回稳定 reason、安全异常类型、HTTP 状态，不返回消息、响应正文、endpoint 或凭据。服务在首次鉴权前不可达为 blocked；鉴权/API 拒绝、SDK 错误或写入后读回超时为 failed。

后续由主任务统一执行。若直接使用独立 CLI，在 backend 工作目录运行以下命令；它只读取进程环境变量，不加载 dotenv 文件：

```powershell
& "..\.venv-integration\Scripts\python.exe" -m evaluation.langfuse_probe --timeout 120
```

执行前需由已有私有配置流程注入本地 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 和 `LANGFUSE_BASE_URL=http://127.0.0.1:13000`；不在命令参数中携带凭据。进程选项设为 `LANGFUSE_TRACING_ENABLED=true`、`OTEL_SDK_DISABLED=false`、`LANGFUSE_MEDIA_UPLOAD_ENABLED=false`、`LANGFUSE_DEBUG=false`、`NO_PROXY=127.0.0.1,localhost`。不要把模型 key 注入探针，不更改生产服务原有环境。

`transport`、`clock`、`sleep` 是显式离线测试注入点，默认不替换真实网络或 SDK。离线测试通过真实 SDK、内存 span exporter 和 HTTP MockTransport 验证序列化及验收判断；这些测试的通过不能计为真实 Langfuse 平台通过。

该入口按一次性验收进程使用。SDK 5.0.4 的 ResourceManager 以 public key 作为进程级 singleton key，shutdown 后不会移除缓存；同一进程用同一 public key 再构造 client 可能复用已关闭的资源。真实重试使用新的 CLI 进程，不复用应用进程；离线 fixture 为每例使用独立的虚构 public 标识隔离 SDK 缓存，不触及真实项目密钥。

## 5. 有界轮询、证据与仍待真实平台的项目

SDK 5.0.4 本地源码 `_client/client.py:2194-2220` 说明 `flush()` 是发送边界，异步 ingestion 后数据通常要 15–30 秒才可查，负载下会更久；这不是 SLA。服务端 `processEventBatch.ts:63-82` 对普通 API 默认最多引入 5 秒入队延迟，在 UTC 23:45–00:15 的边界窗口使用配置值（默认 15 秒）；worker ClickHouse batch interval 默认 1000ms。OTLP v4 走实时 events 路径，score 仍可独立稍晚可见，所以必须轮询**全部所需观测和 score**。

上面探针的 120 秒是本次**读回测试**的上界，从 SDK flush/shutdown 完成后开始。事先的项目鉴权请求最多 5 秒；每个读回 HTTP 请求最多 5 秒且不超过剩余 deadline；默认最多 62 次轮询（仍受同一 deadline 限制），每次间隔最多 2 秒。SDK 提交阶段另有自己的超时/重试，不包含在该 120 秒内。轮询中的 429/5xx/临时传输错误只在原 deadline 内重试，首次项目鉴权遇到不可用服务则直接 blocked；400/401/403/404 等立即失败。200 空数组或部分数据只能继续等待，不是通过。需要更多数据时使用服务端 cursor，不能把首页当成全部结果；此专用两 observation/一 score 探针若出现后页即判异常。

执行后仅保存白名单证据：本地 web/worker 镜像 digest、版本、健康 HTTP 状态、项目匹配布尔值、run_id/case_id/trace_id/observation IDs、score ID/名称/值/subject、父子关系与 usage/脱敏断言结果、轮询次数和耗时、通过/失败/阻塞状态。不要保存 key、Authorization、完整响应、完整 metadata、正文或异常消息。测试数据可留在本次专用项目用于人工复核，不以清空共享卷作为清理方法。

| 尚待执行的验证 | 通过所需证据 |
| --- | --- |
| Headless init 真正可用 | 真容器启动、固定镜像版本/摘要、项目 key 鉴权读取到本次 org/project；日志最多保留成功标记，不输出凭据。 |
| SDK → ingestion → worker → ClickHouse → 公开 API | 同一 trace 下根/子 observation、结束时间、父子关系、同一 run/case 和预期 score 的实际读回；禁止用 MockTransport 或内存 exporter 替代。 |
| 真实服务端字段归一化 | 平台返回的 type、model、completionStartTime、usageDetails、score subject 等与契约一致；当前静态/离线测试不能保证服务器处理成功。 |
| 正常 opt-in 入口 | 新进程设置 `ECHOMIND_TELEMETRY_ENABLED=true` 及本地双 key 后，真实 `get_telemetry()` 得到启用适配器并完成同样读回；本片段显式注入客户端只覆盖绑定接口和 exporter。 |
| 默认正文关闭和脱敏 | 公开 API 真读回时 input/output 无原始正文、合成私密标记不出现、标识符有 HMAC、secret 字段为 REDACTED；仅 SDK 属性断言不够；错误类型/私密异常消息的真实平台验证仍需另测。 |
| 正文 opt-in | 另一次独立合成运行 `trace_content=True` 后实际读回允许的正文及脱敏，不使用客户内容；本最小探针没有覆盖。 |
| 真实场景链路与实际模型 usage | 仍需后续经授权执行场景回放，核对 `evaluation.scenario`、`evaluation.case`、chat/agent/tool/generation 关系及 `task_pass/hard_fail/invalid/case_pass_rate`；本探针的 usage 是手工合成，`model_requests=0`，不能声称验证了模型、检索或完整评测。 |

准备开始时 `backend/evaluation/integration.py` 仅有 preflight/provider/storage 阶段，且其配置加载路径会主动读取 env 文件；本子任务没有执行该 CLI，也没有修改它，Langfuse stage 接入由主任务负责。当前状态保持 **Langfuse 平台待验证 / full_integration_verified=false**。

## 6. 本次静态核对记录

- 已用 `rg` 和定点读取核对上述初始化入口、env schema、key 创建、health/ready、projects、OTLP、observations v2、scores v3、异步队列延迟及 EchoMind adapter/SDK 边界；没有浏览网络。
- 初次 Git 只读命令遇到所有权检查，后续仅用该次命令的 `git -c safe.directory=...` 读取版本，没有修改全局 Git 配置。
- 本次限定修改三个文件：本准备文档、backend/evaluation/langfuse_probe.py、backend/tests/test_langfuse_probe.py；部署与真实平台校验均未执行，不报告为通过。
- TDD RED：新增模块不存在时，专项测试 17 failed（ModuleNotFoundError）；GREEN：17 passed in 2.89s。
- 完整 backend 离线回归：235 passed in 27.97s。命令为项目 Python 3.11 隔离环境执行 pytest -q --tb=short；Windows asyncio 所需的本地 socketpair 权限使用 require_escalated，SDK 只走内存/MockTransport，没有真实平台或模型请求。
- 随后的精确 total 断言回归先单独 RED（错误 total=999 被旧代码判 passed）；核对 SDK 缓存并隔离每例虚构标识后，整份专项正确 RED：1 failed, 17 passed in 4.12s；补 total=5 断言后 GREEN：18 passed in 3.71s。依主任务要求未重复完整回归；235 项结果对应此前版本，最终这次改动的验证为最新 18 项专项。
- 先前准备文档中 Python 示例 AST 与 Compose override YAML 静态解析各 1 项通过；独立模块取代了长示例。
