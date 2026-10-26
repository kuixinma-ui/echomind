# Redis / Chroma / Langfuse 联调边界核对

日期：2026-10-26。初始工作为只读实现与固定版本官方源码核对；随后修复下述 sanitizer 契约并增加真实 SDK 本地验证。未读 `.env`，本子任务未安装依赖，未连接真实模型、Redis、Chroma 或 Langfuse 平台。下列矩阵是验收清单，不表示这些服务已实测通过。

## 已确认的实现边界

- `requirements.txt` 固定 redis 5.2.1、chromadb 0.5.23、anthropic 0.40.0；Langfuse 5.0.4 位于可选 `requirements-observability.txt`。生产 Docker 使用 Python 3.12，满足 Langfuse 的 Python >=3.11 要求。
- 现有 `test_runtime.py` / `test_context_storage.py` 注入 dict/list 存储；fake `expire()` 是空操作，`add/upsert` 直接更新 dict。`test_telemetry.py` 注入自制上下文管理器与 observation。因此它们证明业务隔离、异常处理和调用意图，不能证明服务协议、真实 SDK 序列化、Redis TTL 或后台 exporter。
- 当前情景记忆的 `{"$and": [{"user_id": ...}, {"conv_id": ...}]}` 和用户级 fallback 符合 Chroma 0.5.23 的单操作符 where 规则；ContextRef 写入 Chroma 的值为标量，未发现嵌套 metadata 兼容问题。应保留真实 SDK 验证，不能仅凭这个 fake 判定已联通。[固定版本 where 校验](https://github.com/chroma-core/chroma/blob/0.5.23/chromadb/api/types.py#L548)
- Langfuse 5.0.4 官方源码包含当前使用的构造器 `base_url`、`start_as_current_observation(as_type=...)`、observation `update/score`、客户端 `flush/shutdown`。这里不存在已确定的旧接口删除问题；具体 OTLP、metadata 解码、score 落库仍需后续验收。[固定版本 client](https://github.com/langfuse/langfuse-python/blob/v5.0.4/langfuse/_client/client.py)、[固定版本 observation](https://github.com/langfuse/langfuse-python/blob/v5.0.4/langfuse/_client/span.py)

## 真实联调前必须解决的判据

1. **Chroma 失败可能被本地 fallback 掩盖。** `services/storage.py:30–38` 捕获所有 HttpClient/heartbeat 异常，再建 PersistentClient；错误地址、权限、版本和远端故障都可能得到一个可用的本地库。联调模式必须显式禁止 fallback，或记录并断言实际后端；单看 Runtime 创建成功、知识查到文档都不够。不要把远端 HTTP 协议失败计成本地降级后的通过。
2. **默认 embedding 位于 Python 调用端。** `knowledge_base.py:46–52` 关于服务端生成向量、客户端无需模型的注释与 0.5.23 不符：Collection 在 `_add/_query` 前完成 embedding。当前容器 Dockerfile 已预下载并复制 ONNX 缓存，宿主机首次执行仍可能下载。只检查 heartbeat、collection.count() 不会触及这个依赖。协议测试用确定性 EF；真实检索另验预缓存的默认 EF。[Collection 准备请求](https://github.com/chroma-core/chroma/blob/0.5.23/chromadb/api/models/CollectionCommon.py#L169)、[默认 EF](https://github.com/chroma-core/chroma/blob/0.5.23/chromadb/utils/embedding_functions/__init__.py#L43)、[首次调用与缓存](https://github.com/chroma-core/chroma/blob/0.5.23/chromadb/utils/embedding_functions/onnx_mini_lm_l6_v2.py#L183)
3. **生产 Docker 镜像未包含可选 exporter。** Dockerfile 只 COPY/install `requirements.txt`；即便部署者设置所有 LANGFUSE 变量，当前镜像仍会因缺 SDK 返回 Noop。需要在明确开启 exporter 的镜像变体/构建选项中安装可选依赖，并在验收时断言 `get_telemetry()` 返回 `LangfuseTelemetry`。仅验证客服不报错会掩盖此问题。
4. **Redis 创建是惰性连接。** `MemoryManager.__init__` 的 `redis.from_url` 没有 PING。Runtime 初始化通过不证明凭据、ACL、DB 或网络正确。验收必须调用真实 PING、LPUSH/LRANGE/TTL、SETEX/GET、SCAN/DELETE 与 aclose；TTL 要读服务端值，不能重用现有 fake。[redis-py 5.2.1 构造器](https://github.com/redis/redis-py/blob/v5.2.1/redis/asyncio/client.py#L106)
5. **Langfuse 成功返回不证明成功入库。** adapter 会吞掉 exporter 操作异常，SDK 的传输还可能在后台发生；flush 后服务端读取也有处理延迟。必须捕获真实 trace id，期限内通过 API 或 UI 找到 observation 与 score，并核对父子、类型、usage 与脱敏；超时应失败，不能把未找到记成通过。[官方读取及 ingestion lag 说明](https://github.com/langfuse/langfuse-python/blob/v5.0.4/langfuse/_client/client.py#L395)

## SDK 契约发现与修复

- **已修复：typed datetime。** `_SafeObservation.update` 曾把 `completion_start_time` 的 datetime 变成 `[REDACTED]`。现在仅标准 datetime / None 可跨越该字段边界，其他类型省略，不影响同次合法 usage 更新；任意 metadata 中的 datetime 仍隐藏。真实 Langfuse 5.0.4 内存导出确认字段序列化为 UTC 时间。
- **已修复：真实 Anthropic 内容块。** `AnthropicClientProxy` 传来的真实 `response.content` 曾在 trace_content=true 时仍全变成 `[REDACTED]`。现在只白名单转换 Anthropic 0.40 的精确 TextBlock / ToolUseBlock 类型及声明字段，然后递归脱敏；SDK extras、任意 model_dump/repr 对象不导出。默认 false 仍不导出 input/output，敏感字段、邮箱、手机号、Bearer 和身份 HMAC 规则继续生效。
- 知识库没有配置 `hnsw:space=cosine`，注释却称余弦；`1 - distance` 不应当作经过校准的 0–1 相似度。先检查实际 collection 的度量与数值，再决定阈值；排序本身不因此必然错误。
- 不要看到 Anthropic 风格 `input_tokens/output_tokens` 就擅自改成新字段。本地 Langfuse 参考服务的 `IngestionService.integration.test.ts:651–666` 有此类用量归一化用例；最终应验证目标部署版本的 input/output/cache 三类结果。
- full requirements 与可选 Langfuse 共同解析会带来新的 OpenTelemetry 等传递依赖。固定顶层版本不等于完整锁定；需保存 `pip check`、SDK/import 检查与最终版本清单，不能从 requirements 文本宣称安装兼容。

### RED → GREEN 记录

新增 `backend/tests/test_telemetry_sdk_contract.py`，使用已安装的真实 Anthropic Message/TextBlock/ToolUseBlock、真实 Langfuse 5.0.4、OTel 内存 exporter；score 使用 SDK 自身的后台队列、JSON serializer 与生成的 HTTP client，最外层为 httpx MockTransport。测试拒绝 socket.connect，并使用合成 key、禁用媒体上传；未访问外部平台。

1. 生产代码修改前：`python -m pytest tests/test_telemetry_sdk_contract.py -q --tb=short` → **7 failed, 2 passed in 10.57s**。失败为日期被隐藏、非法日期仍转发和 opt-in 内容块被隐藏；真实 Langfuse 的日期输出亦复现该缺口。
2. 最小修复后：从 backend 执行 `..\.venv\Scripts\python.exe -m pytest tests/test_telemetry.py tests/test_telemetry_sdk_contract.py -q --tb=short` → **35 passed in 8.72s**。
3. Windows 受限执行环境在既有 asyncio 用例处挂起；已终止本子任务的两个 Python 进程，在允许正常本机事件循环的执行环境重跑得到上述结果。全套测试由主任务统一执行。

真实 SDK 验证还覆盖：默认无正文；opt-in 内容有意义且敏感信息仍隐藏；不修改原始 Anthropic 对象；nested observation 的 trace/parent id；generation model/usage；业务异常原样抛出且 exporter 无原始异常正文/栈；score 请求的 traceId/observationId 与 generation 一致。它证明本地 SDK/serializer 契约，不证明 OTLP HTTP 端点、服务端 metadata 解码、平台 score 落库或模型质量。

## 最小验收矩阵

| 层级 / 最小用例 | 必须使用的真实部分 | 可替代的边界 | 通过证据 / 失败判据 |
|---|---|---|---|
| S1：安装与 SDK 导入 | 完整应用依赖 + Langfuse 5.0.4 | 无服务、无凭据 | pip check；导入成功；版本清单；Python >=3.11 |
| S2：Chroma 客户端契约 | 0.5.23 Collection、参数校验与请求准备 | ServerAPI recording double + 确定性 EF | add/upsert/query 产生向量；原生 where 验证拒绝非法多键；版本/范围 metadata 及返回 shape 正确。禁止 ONNX 下载 |
| S3：Langfuse 本地导出 | 5.0.4 实例 + OTel provider/exporter +真实 score serializer | 内存 exporter / httpx MockTransport；合成 key；无公网 | 嵌套与两路并发 trace id/parent id 正确；generation model/usage；异常无正文/栈；score 请求与同一 observation 关联；flush/shutdown 完成 |
| L1：Redis 服务 | 独立测试 Redis + 5.2.1 客户端 | 模型返回固定值 | PING；中文与 metadata JSON 回读；工作/摘要 TTL 在预期范围；同 user/conv 的两个 eval namespace 隔离；清理一个后另一个与哨兵仍存在 |
| L2：Chroma HTTP 服务 | 明确部署的 0.5.23 远端 HTTP 服务 | 确定性 EF；模型返回固定值 | heartbeat/version；禁止本地 fallback；建立三个 eval collection；add/upsert/query/get/delete；跨会话召回、用户隔离、版本/生效时间/scope 选择；只删除所属 collection |
| L3：默认 embedding | 部署使用的 ONNX 缓存与运行用户 | 固定模型答案 | 已断外网仍可文档导入和中文 query；缓存存在且可读；证明首次数据调用不再依赖下载。此项是 embedding 可用性，不能证明业务召回质量 |
| L4：Langfuse 平台 | 独立测试项目，Cloud 或受支持自托管版本 | 合成聊天/模型内容 | API/UI 能读到 trace + score；类型、父子、run/case metadata、usage/cache 正确；默认正文缺失、HMAC 存在；401/超时不破坏业务，同时验收明确失败 |
| M1：真实模型小样本 | 实际 Anthropic Messages 兼容提供商 + 上述已验证存储；Judge 如启用也真实 | 无 | 1 个单会话、1 个工具调用、1 个跨会话任务；成功报告、trace、Token、清理记录相互对应。属于真实模型联调，不能由 S/L 层结果代替 |

S1–S3 只能命名为本地 SDK/协议测试。L1–L4 才是相应服务联调；测试成功不自动等于 M1 或业务准确率已验收。确定性 EF 会绕开本地模型，必须另列 L3，不能在 L2 成功后省略 embedding 的实际部署验证。

## 执行顺序与可运行的预检

依赖安装由主任务统一完成，避免共享 venv 并发安装。安装后从 backend 目录执行以下只读检查；这些检查不会加载 `.env`、实例化客户端或访问服务：

```powershell
..\.venv\Scripts\python.exe -m pip check
..\.venv\Scripts\python.exe -c "import sys, importlib.metadata as m; print(sys.version); print({p:m.version(p) for p in ['anthropic','redis','chromadb','langfuse','pydantic','httpx','opentelemetry-api','opentelemetry-sdk']})"
..\.venv\Scripts\python.exe -c "import inspect; from langfuse import Langfuse; from langfuse._client.span import LangfuseObservationWrapper; from chromadb.api.types import validate_where; print(inspect.signature(Langfuse)); print(inspect.signature(Langfuse.start_as_current_observation)); print(inspect.signature(LangfuseObservationWrapper.update)); validate_where({'\u0024and':[{'user_id':'test'},{'conv_id':'c1'}]}); print('SDK import and where contract OK')"
```

随后先实现 S2/S3，保证 SDK 本地契约稳定；再以显式传入的测试 endpoint 运行 L1/L2/L3，避免 `runtime_config_from_env()` 自动加载 `.env`。每轮使用 `eval_<uuid hex>`，清理前后记录自身集合与受保护哨兵；测试失败仍进入 finally 清理。禁止 FLUSHDB、无范围删 collection、reset 全库。

L4 应用独立测试 key，输出只保留 trace id、observation id、状态和计数，credentials 由调用环境传入。导出后有上限地轮询读 API；限时未落库即记失败。S3 若只使用内存 exporter，没有验证 score 的 HTTP serializer/transport，就必须明确缺失这部分证据。

M1 最后单独执行。不要使用“完整 120 条场景已通过”的表述替代小样本联调记录，也不要把合成答案取得的分数当作真实模型成绩。
