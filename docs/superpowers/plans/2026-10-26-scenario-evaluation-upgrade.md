# EchoMind Scenario Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 建立可信、可回放、可比较的客服场景评测，并补齐上下文治理。

**Architecture:** API 和评测复用 ChatService；场景 runner 在独立运行空间调用该服务，规则与 Judge 分层评分，冻结基线并独立存档。可选 Langfuse 导出阶段观测与实验评分，现有存储增加来源/版本/审计和可控检索策略。

**Tech Stack:** Python 3.12、FastAPI、Anthropic、Redis、ChromaDB、pytest；Vue 3/Vite；可选 Langfuse Python SDK v5。

**Spec:** [设计约束](../specs/2026-10-26-scenario-evaluation-design.md)

## Global Constraints

- 保留现有 API 字段和默认线上存储；离线测试不访问网络或真实存储。
- live 模型回放必须显式选择，不把合成样本或假模型结果标成生产成绩。
- baseline 仅显式提升；除显式声明的实验变量外，不同配置不可直接比较；invalid 不混入质量平均分。
- 场景运行隔离 Redis key、Chroma collection、知识、画像、编排统计和会话。
- 禁用观测或导出异常不影响客服；默认不导出客服全文与原始用户标识。
- 本轮不接真实资金操作、不实现生产认证、不迁移 OpenViking、不 push/部署。
- 修改前先写能暴露缺口的测试，记录 RED/GREEN；每阶段更新本文件与执行记录。

## Review Focus

- 并发请求工具轨迹与用户数据不能串联（任务1/3/5）。
- Judge 无效值、失败和空测试集不能显示成功（任务2/5）。
- 取消、错误与关闭服务后画像任务不能泄漏（任务3）。
- 同一用户的不同评测运行、知识和缓存不能污染线上或互相污染（任务4/5）。
- 遥测依赖缺失、脱敏、异常，以及不同实验配置的比较行为（任务6/8）。

## 执行次序与文件责任

依赖：1、2、3 可按独立文件并行；4 依赖3；5依赖2/3/4；6模块可独立实现，再接入3/1/5；7依赖4；8依赖5/6；9整体验证。

| 任务 | 阶段 | 主要文件 | 状态 |
|---|---|---|---|
| 1 工具轨迹与实体一致性 | P0 | agents/agent_orchestrator.py、tests/test_agent_orchestrator.py | 已完成（离线验收） |
| 2 Judge、冻结基线与报告 | P0 | evaluation/evaluator.py、tests/test_evaluator.py | 已完成（离线验收） |
| 3 共用完整对话服务 | P0 | services/chat_service.py、api/main.py、tests/test_chat_service.py | 已完成（离线验收） |
| 4 隔离运行工厂 | P1 | services/runtime.py、memory/conversation_memory.py、mcp/knowledge_base.py | 已完成（离线验收） |
| 5 场景数据、回放与断言 | P1 | evaluation/scenario*.py、evaluation/datasets/、tests/test_scenarios.py | 已完成（离线验收） |
| 6 可选 Langfuse 观测 | P1 | observability/、requirements-observability.txt、tests/test_telemetry.py | 已完成（离线验收） |
| 7 上下文来源与策略 | P2 | context/、memory/、mcp/、tests/test_context*.py | 已完成（离线验收） |
| 8 API、工作台与实验比较 | P1/P2 | api/main.py、frontend/src/、evaluation/ | 已完成（离线验收） |
| 9 CI、运行指南与总体验证 | 全部 | .github/workflows/ci.yml、docs/evaluation.md、本计划/执行记录 | 已完成（离线验收） |

### Task 1: 工具执行结果与轨迹

**Files:** 修改 `backend/agents/agent_orchestrator.py`；测试 `backend/tests/test_agent_orchestrator.py`。

**Interfaces:** 保持 `AgentResponse`/`OrchestratorResult` 的 tools_used/tool_traces 字段；执行中的工具状态必须属于单次调用。Orchestrator 自行识别意图时补齐 entities。

- [x] 增加正常 tool_use 往返、失败工具、交错并发轨迹和直接编排实体传递测试，确认缺口导致失败。
- [x] 将工具执行状态随调用返回，避免实例 `_last_*` 可变状态；保留白名单、参数验证和最大轮次。
- [x] 运行 `../.venv/Scripts/python.exe -m pytest tests/test_agent_orchestrator.py -q`，预期全部通过。
- [x] 检查 diff 并将证据写入执行记录。

### Task 2: 可信评分与冻结基线

**Files:** 修改 `backend/evaluation/evaluator.py`；新增 `backend/tests/test_evaluator.py`。

**Interfaces:** `EndToEndEvaluator(..., chat_service=None, report_dir=None)`；`run(..., metadata=None)`；`promote_baseline(report)` 显式提升。新增报告 run_id/metadata/invalid/valid_count 字段采用默认值兼容旧代码。

- [x] 测试 Judge 缺字段/NaN/越界/异常、失败不进入均值、冻结基线不覆盖、不同快照不可比、报告独立保存。
- [x] 实现严格评分验证、invalid 单列、证据与期望输入、基线显式提升和运行报告存储。
- [x] 通过 chat_service.chat(..., wait_for_profile=True) 跑对话；兼容无 service 的旧编排模式但标记其覆盖边界。
- [x] 运行 evaluator 测试并记录 RED/GREEN。

### Task 3: ChatService 统一完整链路

**Files:** 新增 `backend/services/chat_service.py`、`backend/tests/test_chat_service.py`；修改 `backend/api/main.py`。

**Interfaces:** `ChatService(orchestrator, memory, telemetry=None)`；`async chat(message, user_id, conv_id=None, *, wait_for_profile=False) -> dict`；`async close()`。返回旧 ChatResponse 字段，加 orchestrator_latency_ms/stage_latency_ms/tool_traces。

- [x] 使用可控时钟及内存依赖测试历史/实体传递、读写顺序、画像等待、任务错误/取消/关闭、完整计时。
- [x] 实现服务和受管理画像任务；API `/chat` 只调用共用服务。
- [x] 保留 HTTP 接口契约测试，验证 API 与直接 service 调用行为一致。
- [x] 运行服务和 API 测试，记录证据。

### Task 4: 隔离评测运行时

**Files:** 新增 `backend/services/runtime.py`、`backend/tests/test_runtime.py`；修改 MemoryManager/KnowledgeBase 初始化接口。

**Interfaces:** `async create_runtime(config, *, namespace='', knowledge_documents=None, telemetry=None)` 返回包含 `service`、`orchestrator`、`knowledge` 和 `async close(cleanup=False)` 的 Runtime；评测命名空间为 `eval_<run_uuid>`。底层允许注入 Redis/Chroma 客户端，默认 collection 名不变。中途失败或取消必须回收已获取资源并报告回滚状态。

- [x] 测试线上默认名保持、两个运行 key/collection 分离、知识种子关闭、异常关闭与精确清理。
- [x] 实现工厂，复用现有生产 RAG/工具装配；评测使用独立编排器和固定知识文档。
- [x] API 旧评测调用也改用独立 runtime；生成唯一 run_id，清理仅本次资源。
- [x] 验证不能通过评测路径删除默认线上 collection。

### Task 5: 场景数据与回放执行器

**Files:** 新增 `backend/evaluation/scenarios.py`、`scenario_runner.py`、`datasets/customer_service_v1.json`、相关测试。

**Interfaces:** dataset 顶层 `id/version/status/knowledge/cases`；case 包含 `id/scenario/turns/initial_state/assertions/budgets`。`ScenarioRunner(service_factory, judge=None, telemetry=None).run(dataset, *, metadata=None)` 返回可 JSON 化报告；所有 attempted 进入分母。CLI 默认 validate，live 显式执行真实 runtime。

- [x] 测试数据格式错误、未知规则、八类/120条/十九意图覆盖、任务硬失败、invalid、超时继续、运行与用户隔离。
- [x] 编写八组可读合成用例及固定政策，每条用例包含可执行断言；状态标为 synthetic_pending_review。
- [x] 初始 prior 会话显式 archive_session，memory_recalled 验证证据ID；Judge 获取初始事实和具体记忆期望；删除跨会话记忆后任务必须失败。
- [x] 实现路由/工具/字段/证据/升级/边界等确定性断言和可选证据 Judge，输出 task/turn/scenario、延迟、用量及失败原因。
- [x] 运行数据校验和离线回放测试；提供 live 命令但不自动访问真实模型。

### Task 6: 可选 Langfuse 遥测

**Files:** 新增 `backend/observability/telemetry.py`、`backend/requirements-observability.txt`、测试；接入 ChatService/Agent/检索/runner。

**Interfaces:** `get_telemetry()` 默认 Noop；`Telemetry.span(name, *, kind='span', input=None, metadata=None)` 返回 context manager，yield observation.update(**fields)/score(name,value)；`flush()`/`shutdown()`；显式 enabled 才加载 SDK。

- [x] 测试禁用时不导入 SDK、导出异常不影响请求、嵌套及并发父子关联、默认隐藏正文并散列身份、flush。
- [x] 核实 SDK v5 可用接口与确切版本，提供独立可选依赖；配置从环境变量读取。
- [x] 对完整请求、memory、intent、Agent、LLM、tool、retrieval/rerank 记录观测；根记录必要结构摘要，实验评分关联同一 run。
- [x] 验证无配置仍通过全套离线测试。

### Task 7: ContextRef 与可控检索

**Files:** 新增 `backend/context/`、测试；修改 KnowledgeBase、MemoryManager、MCPToolManager。

**Interfaces:** ContextRef 包含 id/type/version/source/scope/effective_at；检索结果保留 evidence_id 与来源。检索策略 `always` 为兼容默认，新增 `never`、`adaptive`，分别控制 rewrite/rerank，实验元数据固定其值。

- [x] 测试文档版本/来源保留、相同文档不同版本可定位、用户画像更正的新增/修改/删除审计与隔离。
- [x] 实现兼容元数据和记忆差异记录，保留原文来源，不将模型推断自动标为业务事实。
- [x] 测试策略开关、重排实际回退标记、空召回/超时，并记录调用次数供消融对比。
- [x] 增加同快照配对比较，配置不一致明确拒绝比较，性能数字只来自实际运行。

### Task 8: API 和评测工作台

**Files:** 修改 `backend/api/main.py`、`frontend/src/App.vue`、`frontend/src/lib/backends.js`，必要时新建独立组件。

**Interfaces:** GET `/eval/scenarios`、POST `/eval/scenarios/run`、GET `/eval/runs`、GET `/eval/runs/{run_id}`、POST `/eval/compare`；回放默认 validate，真实执行需 mode=live。基线提升仅 CLI，避免未鉴权管理接口。

- [x] 测试参数验证、未知run、路径穿越拒绝、历史报告、不同配置拒比和兼容旧接口。
- [x] 增加场景分组、数据状态、运行模式、invalid/硬失败、逐项失败定位、历史与对比展示；错误显示在页内。
- [x] 用 Node 原生 test 测试前端请求/结果映射；`npm run build` 验证构建。
- [x] 默认按钮不触发付费调用；界面明确合成用例待审核。

### Task 9: 交付验收

- [x] CI 安装最小测试依赖，跑后端全套离线测试、场景校验、前端测试与构建。
- [x] 编写 `docs/evaluation.md`：本地/CI/live命令、隔离/清理、基线提升、Langfuse配置、数据口径、OpenViking后续影子试验。
- [x] 新审查者检查最终 diff 的规格符合性、并发隔离、异常路径和测试真实性；修复重要发现。
- [x] 运行最终验证并更新本计划、执行记录；单列未执行的真实第三方联调及人工审核。

## 验证命令（仓库根目录）

```powershell
Push-Location backend
../.venv/Scripts/python.exe -m pytest tests -q
../.venv/Scripts/python.exe -m evaluation.scenario_runner --mode validate
Pop-Location
Push-Location frontend
npm test
npm run build
Pop-Location
git diff --check
```


## 可选后续阶段（本轮不默认执行）

- [ ] OpenViking server-only 侧车、脱敏副本双读、session task 等待和 memory diff 对比。
- [ ] 业务专家审定数据与 Judge 校准，独立 held-out 集评估。
- [ ] 生产身份、ACL、审计及备份；基于配对实测再决策存储迁移。
