# Task 5 场景数据与执行器交付记录

本任务只新增 `backend/evaluation/scenarios.py`、`scenario_runner.py`、`report_store.py`、`datasets/customer_service_v1.json` 和对应两个专项测试文件。没有修改其他 agent 负责的服务、编排、记忆、MCP、API 或前端；没有提交、推送、部署或触发真实模型。

## 数据与校验

数据状态固定为 `synthetic_pending_review`，120 条人工可读合成用例，八组各 15 条，135 个对话轮次，覆盖现有十九种 IntentCategory。九份固定知识均声明为演示政策，不是实际业务承诺。场景包括基本分诊、订单物流、账单退款、技术与安全、多轮补充、跨会话更正与记忆、人工升级与权限边界、知识证据及未知政策。

场景输入不接受预填 `response`。每轮必须调用实际注入 Runtime 的 `service.chat(..., wait_for_profile=True)`；只有专项测试使用显式可控的内存 Runtime，不把这些测试结果当成模型成绩。

`load_dataset(path=DEFAULT_DATASET) -> dict` 加载并调用严格校验。`validate_dataset(dataset) -> dict` 返回 id/version/status/case_count/turn_count/assertion_count/scenario_count/scenarios/intents/fingerprint。顶层、case、turn、initial_state、budget 和 rule 均拒绝未知字段；拒绝未知意图、规则、重复 ID、空用例、缺少必需字段、越界 turn、非有限数和不明确断言范围。

断言只允许一个明确目标：`turn: 0`（从零起算）或者 `scope: task`。task 的结构目标包含 `turns`（实际 ChatService 字典列表）、合并的工具轨迹、tools_used、回答及总服务耗时。例如 `field_equals` 的 task 路径 `turns.0.intent`。没有“最后一轮猜测”或隐式平均规则。

可用规则如下：

| type | 必填配置 | 判定依据 |
|---|---|---|
| intent / agent | expected | 实际 intent / primary_agent |
| tool_called | tool | 实际工具轨迹 success，默认 true；可显式检查失败调用 |
| tool_absent | tool | 工具轨迹与 tools_used 均不出现 |
| tool_argument | tool/path/expected | 执行轨迹 input 的结构路径 |
| tool_result | tool/path/expected | 执行轨迹 result_summary 的结构路径 |
| field_equals / field_present | path，equals 另需 expected | 实际响应的结构路径 |
| evidence | expected 文档 ID 列表 | 成功检索的 document_ids；可另配 evidence_ids 严查 ctx_ 版本切片 |
| semantic | expected/evidence | Judge 接收实际回答、全部或指定轮问题、固定知识证据 |
| latency | max_ms | 实际服务响应耗时字段 |

`required` 默认为 true。所有 required 断言及服务轮次均成功才是任务通过；任何 `hard: true` 的失败均为 hard_fail，其他通过项不能将其平均成通过。语义规则默认四项 Judge 分数分别达到 0.75，综合均分仅展示；accuracy=0 不会被另外三项满分掩盖。Judge 缺失、缺分、NaN、越界、抛错都记 invalid，超时单列，不填 0.5、不用正则替代政策判断。固定知识证据缺失在数据校验时即拒绝。

## Runner 与报告接口

`ScenarioRunner(service_factory, judge=None, telemetry=None).run(dataset, *, metadata=None)` 返回 JSON 字典。factory 可同步或异步，参数只有 `namespace=eval_<32hex>` 与 `knowledge_documents=dataset['knowledge']`。每次 run 创建一个新 Runtime；每个 case 使用独立用户与会话前缀。`initial_state.messages` 支持 `{session, role, content}`；prior/current session 同用户而会话不同，用 `memory.add_message` 播种，不需要画像 seed。

逐轮 timeout/error 后继续处理后续轮次与 case。运行工厂或初始状态失败会为受影响的 case 与所有计划轮次保留错误记录；`finally` 总会对已经创建的 Runtime 执行 `close(cleanup=True)`。清理错误可见且总报告 status 为 error，不能伪装运行正常结束。

报告包含：

- `run_id/namespace/timestamp/mode/status/dataset/metadata/cleanup`。
- `cases[]` 的 id/scenario/description/user_id/status/turns/assertions，及 invalid/timeout/error_count/hard_fail 标记。
- `turns[]` 的 index/message/expected_intent/conv_id/executed/status/latency_ms/actual 或 error_detail。
- `assertions[]` 的 id/type/turn 或 scope/required/hard/status/reason/scores。
- `summary.cases/turns/assertions` 和 `by_scenario`，各保留全部选中任务的 attempted 分母，并单列失败、invalid、timeout、error、hard_fail；turns 另列实际 executed 数量。
- 兼容顶层 `total/passed/pass_rate/invalid/timeout/error/hard_fail/valid_count`。case 标记可重叠（例如既发生技术错误又有不可评估语义），不能把各标记相加当作互斥分区。
- `latency` 包含真实成功返回服务输出的 sample_count/valid_count/mean_ms/max_ms/p50_ms/p95_ms，采用线性插值，少于 20 个样本标 `small_sample_estimate=true`；零样本值为 null。质量判断无效不会丢掉实际观察到的服务耗时。
- evidence 规则 `scores.recall` 按期望文档和版本切片命中比例计算；报告 `evidence_recall.mean/valid_count` 只汇总可评估规则，缺失时 mean 为 null。
- `usage` 只累计实际响应提供的有限数值，不存在 token 数据时明确 unavailable。没有伪造成本或 token 数。

runner 本身返回报告，CLI 和 API 调用方显式调用 `ReportStore.save(report)`。观测仅导出 run/dataset/summary 与评分元数据，导出异常不会改变报告结果。

## 报告存档、比较与基线

`ReportStore(report_dir=DEFAULT_REPORT_DIR)` 提供 `save(report)/load(run_id)/list()/compare(left,right,*,vary=())/promote_baseline(run_id)/baseline()`。默认目录为已忽略的 `backend/data/eval/scenarios`，可用 `ECHOMIND_EVAL_REPORT_DIR` 覆盖。save 使用独占创建拒绝覆盖；run_id 不接受路径、点号、Windows 设备名和保留的 baseline 名。

`build_fingerprints(dataset, config)` 只产生八个安全快照指纹，不复制 api_key 或全量 config。包含 dataset、默认及各 Agent 模型/env 参数与端点哈希、实际 Agent Prompt 源码与 Skills 截断配置、Skills 文件、固定知识、实际后端 Python 源码、Judge 实现/是否启用/模型、检索 rewrite/rerank/top_k/timeout/adaptive_rules_version=1。代码哈希基于工作区文件，能识别尚未提交修改，动态工具统计不进入固定指纹。

默认全部八字段相同才可比。显式 vary 只允许 model/prompt/skills/code/retrieval；dataset/knowledge/judge 始终固定。缺指纹、未声明变化、运行模式不同、验证报告、无任务或 case ID 无法配对时拒绝给出收益差值。可比较时返回 changed_factors/controlled_factors、总体 deltas、逐 case 的左右状态、耗时差值及逐断言状态。无效数据与运行错误仍保留原报告，不从分母过滤。

基线只通过 `promote_baseline` 或 CLI 的显式选项提升，普通保存或回放不会修改。不同主模型实验需要保持实际 Judge 模型固定，否则 Judge 指纹变化会正确拒绝比较。

## CLI

在 backend 目录运行：

```powershell
../.venv/Scripts/python.exe -m evaluation.scenario_runner
../.venv/Scripts/python.exe -m evaluation.scenario_runner --mode validate
../.venv/Scripts/python.exe -m evaluation.scenario_runner --mode live --limit 5 --scenario billing_refund --judge
../.venv/Scripts/python.exe -m evaluation.scenario_runner --promote-baseline <run_id>
../.venv/Scripts/python.exe -m evaluation.scenario_runner --compare <left_run_id> <right_run_id> --vary retrieval
```

默认 validate 仅用标准库加载、完整校验 120 条，不加载真实 runtime、不调用模型或数据库。只有 live 显式调用生产 `services.runtime.create_runtime(runtime_config_from_env(), ...)`，缺少 key 先拒绝；live 默认最多五条，`--limit` 可显式调整，`--scenario` 可重复。`--judge` 另显式启用模型语义评分；未启用的语义项目为 invalid。live 使用筛选后真实子集的 dataset 指纹，不能拿不同子集当同一数据比较。

## TDD 与验证证据

1. 首次新增测试后执行专项：两个 collection error，分别为 `evaluation.scenarios`、`evaluation.report_store` 模块不存在（RED）。
2. 首轮发现环境未装 pytest-asyncio，异步标记会跳过；立即改为仓库使用的 `asyncio.run` 同步测试入口。之后报告中的全部通过数均无 skipped。
3. 显式 vary 与 case 配对测试先出现 2 failed / 15 passed（不支持 vary；错误地允许不同 case 集），实现后 17 passed（GREEN）。
4. CLI status、检索配置指纹、文档与切片证据、保留 baseline 名、live 限量测试先出现 5 failed / 17 passed，补齐后 22 passed（GREEN）。
5. 延迟分位数、证据 recall 与语义 accuracy 硬失败先报缺少 `_latency_summary` 的 collection error（RED），实现后 24 passed（GREEN）。
6. CLI 实际 Judge 固定模型测试先出现 1 failed / 24 passed（仍使用聊天变体），改为 config.judge_model 优先后最终专项 25 passed（GREEN）。固定 Judge 可由 runtime config 的 ECHOMIND_JUDGE_MODEL 提供。

所有专项使用以下命令，在 Windows asyncio 所需的 require_escalated 环境执行：

```powershell
../.venv/Scripts/python.exe -m pytest tests/test_scenarios.py tests/test_report_store.py -q
```

还执行完整默认数据 validate，确认 120 cases / 135 turns / 8 scenarios / 19 intents。未运行全套后端测试，其他并行任务仍在各自 TDD；总体验证交给主任务统一进行。

未执行：真实模型、Redis、Chroma、Langfuse 服务联调，以及业务人员审核标签、Judge 校准、生产准确率验证。所有数据与规则仍待业务确认。
