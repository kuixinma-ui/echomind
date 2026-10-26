# 重启后真实联调结果（2026-10-26）

**结论：选定的3条真实端到端smoke通过。** Redis、Chroma HTTP、默认ONNX embedding及Langfuse平台写入/读回已实测；这不等于120条全量评测或业务审核完成。未提升baseline，未提交/推送代码。

## 环境

| 项目 | 实测 |
| --- | --- |
| WSL | 3.0.3.0，kernel6.18.54.1-1；用户重启后status退出0，默认WSL2 |
| Docker Desktop / Engine | 4.96.3 / 29.9.2；缓存安装包SHA256与官方一致、签名Valid，重启后安装退出0 |
| Compose | v5.6.1；原生config --quiet通过 |
| 本机后端 | Python3.11独立.venv-integration，Anthropic0.40.0、Chroma0.5.23、Redis客户端5.2.1、Langfuse SDK5.0.4 |
| 模型 / Judge | 现有DeepSeek v4-pro配置；凭据只在回放进程读取，未复制到项目配置或Git |
| 部署 | 独立echomind-integration项目，Redis/Chroma与Langfuse六组件，共8个服务healthy |
| 端口 | 仅127.0.0.1：Redis16379、Chroma18001、Langfuse13000、MinIO19090 |

Docker本次使用8个CPU、约8.2GB内存上限。没有由代理接受Docker协议或再次重启系统。服务来源、镜像digest、端口与健康状态保存在 `backend/data/integration/setup/compose-services.jsonl`；没有归档容器环境中的密钥。

## 真实组件验收

| 验收 | 结果及证据 |
| --- | --- |
| Redis | 7.4.2，PING、列表读写、实际TTL60秒、键隔离、UUID范围清理均通过；结合Docker镜像/端口来源确认，非fakeredis |
| Chroma HTTP | 0.5.23，heartbeat/version、唯一集合、显式向量写入、用户/会话过滤、删除通过；无本地fallback |
| 默认embedding | 官方缓存archive SHA256通过；安全解压，禁止Python网络连接时推理得到384维向量；实际中文文本HTTP写入/查询、跨用户过滤、哨兵与清理均通过 |
| Langfuse平台 | 4.70.0，真实SDK→OTLP/score→worker→公开API读回；root/generation、父子、model/usage、时间、默认无正文、HMAC/敏感字段脱敏、score subject匹配全部通过 |

对应证据：

- `backend/data/integration/integration_8d5f3505b2ce4f28b8536de2e6f11cbf.json`：真实Redis+Chroma；0模型调用。
- `backend/data/integration/setup/embedding-cache-check.json`、`chroma-default-embedding-http.json`：默认模型缓存及HTTP文本回环。
- `backend/data/integration/integration_7913bd6147e94508a4fa2bbf0c4e7ca4.json`：真实Langfuse读回，约10.3秒、4次轮询；使用合成usage2/3验证传输，实际LLM调用0次。

这些文件均被Git忽略。默认embedding单文档回环验证可用性，不代表检索质量已在真实业务数据上校准。

## 三条真实场景

使用独立子集 `customer_service_smoke_v1`，保留原知识快照及synthetic_pending_review状态，3条/3轮/17断言；子集fingerprint为 `7b768d32e198e025bbeffa196603505dacbc34d96cab8650b1e7c84fa8117ab8`。

| 样例 | 真实验收点 | 修复后 |
| --- | --- | --- |
| basic_routing_01 | 问候意图、general节点、实际回复、时延 | passed |
| knowledge_boundaries_04 | invoice路由、真实知识工具与invoice来源、固定Judge的政策/演示边界判断 | passed |
| cross_session_memory_01 | prior会话显式归档、实际记忆证据ID、利用安卓14历史、固定Judge | passed |

修复后run_id：`f9e8f35ed49c48868a99d98e89c0b401`。结果：**3/3 passed，invalid/timeout/error/hard_fail均0，cleanup=completed**，本次合计约73.42秒。

Provider返回的用量分别保留：聊天链路13次SDK create，input5575/output1819/cache_read3840；Judge额外2次，input1862/output86。SDK内部HTTP重试不额外计入create次数，不能据此精确推断账单。没有把合成传输探针的usage计为真实模型用量。

报告：`backend/data/eval/scenarios/f9e8f35ed49c48868a99d98e89c0b401.json`。

## 首轮问题及修复依据

首轮run_id `f5d4b331035144f2a9a2875e977ef91b`：1通过、2个Judge invalid，0超时/执行错误，清理完成。真实工具来源与跨会话证据断言已通过，未将无效Judge填成默认分数或通过。

实际对照确认：同一发票Judge提示，DeepSeek默认thinking在256输出Token处以max_tokens结束，只返回thinking、无text。显式disabled后有text并正常end_turn。诊断只保存块类型/长度/停止原因/用量，不保存推理正文。官方[Anthropic兼容说明](https://api-docs.deepseek.com/guides/anthropic_api/)支持thinking参数；SDK0.40使用extra_body发送该扩展。

最小修复：

- `ECHOMIND_STRUCTURED_THINKING` 默认provider_default，不发送扩展；本次DeepSeek联调配置明确选disabled。
- 仅Judge、意图JSON、画像、查询改写、重排五处使用该策略；单调用256/512上限和普通Agent生成设置保持原行为。
- 共用安全JSON解析，处理text块/code fence，区分empty_text/max_tokens/invalid_json/wrong_type；四分数、画像类型及重排完整排列验证继续保留。
- Judge与retrieval指纹包含策略及解析实现。首轮与修复后报告不能当作Judge控制不变的性能A/B；没有据此宣称统计收益。
- 修复前专项18失败/1通过，修复后19通过，相关回归140通过；最终全套结果以JUnit和本阶段执行记录为准。
- 最终完整后端：**257 passed in53.22s，无skipped**，JUnit `backend/data/integration/pytest-python311.xml`。

首轮失败报告、读回JSON和诊断均保留，未覆盖、未提升baseline。修复后回放使用同三条样例，未扩大到120条。

另修复真实环境暴露的问题：

- 当前Codex旧进程未继承新Docker PATH，凭据助手不可见；仅在执行进程补充Docker bin，未删除或更改全局凭据配置。
- Windows出站代理会影响localhost SDK连接，联调配置明确NO_PROXY；默认embedding回环的网络守卫只允许本机18001。
- Langfuse镜像内进程监听容器主机地址，127.0.0.1健康检查实际Connection refused。改为容器自身HOSTNAME，使用Compose的$$正确转义；实际web/worker检查退出0，8服务均healthy，保留首次up的失败日志。

## 场景观测读回

修复后每条case的EVALUATOR根节点、GENERATION、task_pass=1/hard_fail=0/invalid=0及score subject均从平台实际读回，所有节点input/output默认缺省。三个trace的SDK generation分别3、7、5，合计15，与聊天13+Judge2对齐。

`retrieval.rewrite` / `retrieval.rerank`还有各一层语义GENERATION节点，因此全部GENERATION节点为17；不能把节点总数当成HTTP请求数。按 `name=anthropic.messages.create` 与usage.model_calls统计SDK调用。独立只读审查已确认没有画像用量丢失。

证据：`backend/data/integration/readback_f9e8f35ed49c48868a99d98e89c0b401.json`。首轮invalid评分也已独立读回，证明失败状态没有被观测层改写。

## 保留与后续

- 本机8个服务保留运行，独立数据卷保留。仅创建了本地测试组织/项目/API key，未创建外部账户或UI用户；当前以API完成验收。若需界面查看，可另配置本地UI用户。
- 生成的.env.integration有本地服务秘密，被Git忽略；不得打印compose config展开内容或分享该文件。
- 120条仍需处理已公开的标注冲突及业务审核，再决定全量模型回放。OpenViking迁移、hosted dataset同步、负载/生产部署验收继续后置。
- 可选Linux app镜像尚未构建；本次实际端到端是Windows Python3.11后端连接Docker依赖。前端本阶段未改，沿用上一阶段验证。

正常停止（保留数据卷）：

```powershell
Set-Location D:\project\echomind\github-release\EchoMind\backend
docker compose --env-file .env.integration -f compose.integration.yml -f compose.langfuse-headless.yml --profile langfuse down
```

恢复时用同一配置执行up -d --wait，避免去掉headless override造成不必要的服务配置变化。完整操作见[联调指南](../../integration.md)。
