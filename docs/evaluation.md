# 场景评测与上下文实验

入口是工作台顶部的“评测”。先校验场景数据，再显式回放模型；查看失败后修改一个实验因素，用相同场景比较调优前后的结果。

## 三种验证口径

| 方式 | 实际执行内容 | 能证明什么 |
|---|---|---|
| 数据校验（默认） | 读取 JSON，验证用例、断言、知识引用和覆盖 | 场景数据符合执行契约；不产生质量成绩 |
| 离线单元/集成测试 | 真实业务代码 + 注入的存储/模型边界 | 轨迹、隔离、异常处理、断言、计时及报告逻辑 |
| live 回放 | 独立 Runtime、Redis/Chroma 空间、真实模型与可选 Judge | 当前模型及知识快照在指定场景上的实际表现 |

首批 `backend/evaluation/datasets/customer_service_v1.json` 为 120 条合成场景，覆盖八组、十九类意图，状态为 `synthetic_pending_review`。演示政策不是实际业务承诺。投产质量门槛需要业务审核、独立验收集和人工校准，不能用离线假模型测试成绩代替。

## 本地验证（PowerShell）

以下命令从仓库根目录执行，Python 环境位于项目内 `.venv`。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-test.txt
Push-Location backend
..\.venv\Scripts\python.exe -m pytest tests -q
..\.venv\Scripts\python.exe -m evaluation.scenario_runner --mode validate
Pop-Location
Push-Location frontend
npm ci
npm test
npm run build
Pop-Location
```

Linux CI 使用相同依赖与测试。测试依赖文件通过 constraints 引用生产版本，不安装 Chroma embedding 运行时，不连接模型或外部存储。

Windows 受限沙箱可能阻止 `asyncio` 初始化所需的本机资源；本次测试在允许正常本机事件循环的执行环境完成。普通本机终端无需额外 pytest 插件。

## 只启动数据校验工作台

未配置模型时可启动评测专用模式，不创建线上会话组件。

```powershell
Push-Location backend
$env:ECHOMIND_EVALUATION_ONLY='true'
..\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

另一终端在 `frontend` 执行 `npm run dev`。评测页默认执行数据校验；此模式下 `/chat` 不可用。正常客服部署将 `ECHOMIND_EVALUATION_ONLY=false` 并按项目开发指南配置完整依赖。

## 显式 live 回放

先安装 `backend/requirements.txt`，配置模型、Redis、Chroma 和可调用的模型名。环境变量可通过 `.env` 配置；不要提交凭据。Redis/Chroma 的网络连接和 embedding 模型必须可用。

```powershell
Push-Location backend
..\.venv\Scripts\python.exe -m evaluation.scenario_runner --mode live --judge --limit 5
```

`--mode live` 明确授权当前命令调用配置的模型；`--judge` 另启语义裁判。默认最多回放五条；可用 `--limit 120` 或重复 `--scenario <分组>` 明确扩展。省略 Judge 的语义断言记为 invalid，不能显示通过。API/工作台选择 live 时会运行证据 Judge。

模型变化实验建议固定 `ECHOMIND_JUDGE_MODEL`，避免裁判同时变化。模型服务需要支持 Anthropic Messages 协议。

## 可解释Judge与固定回复诊断

默认 `ECHOMIND_JUDGE_MODE=scores` 保留旧四分数协议。显式选择 `explained_v1`（或场景CLI的 `--judge-mode explained_v1`）会要求每维同时返回criterion、简短reason、输入证据ID和missing清单；四个分数和原通过门槛不变。解释缺字段、越界、未知证据ID或无效JSON都会记invalid，不能补默认分。输出上限由256变为1600，模式与预算、实际实现均进入Judge指纹；两种裁判不能混作同条件结果。

校准工具可只消费已保存的回复，默认离线验证，不创建客服Runtime/Redis/Chroma，也不重新生成客服回答：

```powershell
# backend目录；source-batch需含完整manifest、dataset与分组原报告
..\.venv-integration\Scripts\python.exe -m evaluation.judge_calibration --source-batch data/eval/batches/repair_acceptance_20261027_01
```

输出包含固定输入、原报告hash/评分引用和快照hash。默认最多12个semantic断言；更大源批次需重复 `--case CASE_ID` 显式选择。缺失源回复仍在分母中记source_unavailable。只有摘要的环境不具备完整原始批次，应迁移完整批次目录或从新实验产生真实报告。

在已配置模型的本机.env.integration中显式发起Judge-only调用（会产生模型费用）：

```powershell
..\.venv-integration\Scripts\python.exe -m evaluation.judge_calibration --source-batch data/eval/batches/repair_acceptance_20261027_01 --mode live --env-file .env.integration --output-dir data/eval/calibrations/my-explained-run --max-model-calls 12 --max-model-seconds 600 --max-workers 2
```

输出目录必须不存在；已有或中断目录不会自动再次收费。每项只评一次，已返回用量在格式错误时也保留；SDK内部HTTP重试不单独计数。结果是新裁判对固定回复的诊断，解释仍需人工审定，不代表客服准确率提升，不覆盖原报告或baseline。ScenarioRunner仍按四维逐一达标；兼容的旧EndToEndEvaluator保留原overall门槛。

## 有界分组与断点续跑

`evaluation.batch_runner` 默认仅离线校验。显式 live 时按场景分组，最多两组同时执行，每组默认最多 200 次 SDK create 尝试、1200 秒模型等待；Judge 和画像也计数。SDK 内部 HTTP 重试不单独计数，初始化/存储/清理不受整体墙钟硬超时保证，这些限制不等于金额硬上限。

在 backend 目录、已完成本机 `.env.integration` 配置与服务预检后，先执行首组（会调用真实模型）：

```powershell
..\.venv-integration\Scripts\python.exe -c "from dotenv import load_dotenv; load_dotenv('.env.integration', override=True); from evaluation.batch_runner import main; raise SystemExit(main())" --mode live --dataset evaluation/datasets/customer_service_v1_1_candidate.json --batch-dir data/eval/batches/my-candidate-batch --scenario basic_routing --max-workers 2 --judge
```

检查首组后恢复剩余分组，沿用相同数据、代码、模型、裁判、Skills、知识、检索策略和预算：

```powershell
..\.venv-integration\Scripts\python.exe -c "from dotenv import load_dotenv; load_dotenv('.env.integration', override=True); from evaluation.batch_runner import main; raise SystemExit(main())" --mode live --dataset evaluation/datasets/customer_service_v1_1_candidate.json --resume data/eval/batches/my-candidate-batch --max-workers 2 --judge
```

批次保存完整 dataset 副本、manifest、各组报告及校验哈希，使用独占锁和原子写入。断言失败或预算中断的组也视为已执行，恢复时不会自动再次收费回放。代码/配置指纹变化会在新模型调用前拒绝恢复；运行中丢失进程、遗留锁或 started/blocked 组需先调查，不自动猜测是否已付费或删除锁。

可离线核对完整覆盖与缓存报告，无需原模型密钥：

```powershell
..\.venv-integration\Scripts\python.exe -m evaluation.batch_runner --aggregate data/eval/batches/my-candidate-batch
```

汇总始终保留全部 120 条候选的主分母，pending/blocked 单列。`completed` 仅表示所有组已归档；通过、失败、invalid、timeout、error、hard_fail、预算中断和清理证据需分别检查。批次报告位于 `data/eval/batches`，不会自动进入现有工作台 `data/eval/scenarios` 历史列表或提升基线。运行目录中的临时 Python 驱动不再影响 code 指纹，实际应用源码和 Skill 资源仍纳入快照。

## API 与报告

| 路径 | 行为 |
|---|---|
| `GET /eval/scenarios` | 场景目录、数据状态、覆盖统计 |
| `POST /eval/scenarios/run` | 默认 validate；body 可传 mode、scenario、limit |
| `GET /eval/runs` | 已保存运行的摘要 |
| `GET /eval/runs/{run_id}` | 单次完整报告，仅接受安全运行标识 |
| `POST /eval/compare` | 配对比较两个运行，显式声明变化因素 |
| `POST /eval/run` | 兼容旧评测入口，现使用隔离 Runtime；该旧入口为显式真实模型评测 |

默认报告目录 `backend/data/eval/scenarios`，可通过 `ECHOMIND_EVAL_REPORT_DIR` 更改。报告只追加、不自动覆盖同名运行；目录已经被 Git 忽略。报告可能包含用于诊断的对话和错误，存储访问控制由部署者提供。

每个 live 运行生成 `eval_<uuid>` 独立命名空间；每个用例又有独立用户/会话。Redis key、Chroma 知识/记忆/画像、工具缓存、Agent 统计都与线上独立。异步工厂逐步获取资源；中途失败也回滚并报告 rolled_back/rollback_failed。结束时取消遗留画像任务，清理只属于本次运行的资源并关闭客户端；清理失败会在报告中明确记录，不能当作正常完成。

## 分层评分

- `total` 为全部尝试的任务数；超时、执行错误和 invalid 仍在主分母内。
- `summary` 分别统计任务、轮次和断言；任务通过要求全部必选断言和轮次通过。
- `hard_fail` 为硬性业务约束失败；多个高分不能抵消它。
- Judge 必须提供完整、有限、0–1 的四维分值；无效评分不进入均值。
- 路由、工具白名单/参数、必要字段和结构化工具结果用代码判断；语义政策合规使用固定证据及明确期望的 Judge。
- `document_ids` 表示文档级命中，`evidence_ids` 表示版本/切片级证据，`context_refs` 保留来源和版本；不能混淆两种召回口径。
- 跨会话种子先 `archive_session` 到情景记忆，再用 `memory_recalled` 校验实际召回的内容指纹；Judge 另外评判具体事实与当前更正。仅有“回答非空”不能算记忆正确。
- 延迟记录完整服务耗时及阶段耗时。报告的 p50/p95 附样本数，少量样本只是估计。
- Token 只累计真实 provider usage：输入、输出、缓存读取、缓存创建、调用次数。没有价格表时不推算美元费用。线上 `usage_scope=through_response` 不含响应之后的画像调用；评测等待画像完成，记录 `including_profile`。

## 基线与可比性

实验指纹包括 dataset、model、prompt、skills、knowledge、code、judge、retrieval。代码指纹读取实际源码，包含未提交的实现；配置快照不会包含 API Key。

默认严格比较全部条件。需要研究代码、模型、提示词、业务规则或检索策略时，用 `--vary` 或 API body 的 `vary` 显式声明；数据、知识和裁判始终是控制条件。不同用例集合或缺失指纹不输出可比结论。

```powershell
# 仅对已保存的实际回放进行比较；不再次调用模型。
..\.venv\Scripts\python.exe -m evaluation.scenario_runner --compare BASE_RUN CANDIDATE_RUN --vary code retrieval
# 人工审核一个运行后，显式将其冻结为基线。
..\.venv\Scripts\python.exe -m evaluation.scenario_runner --promote-baseline RUN_ID
```

旧 `/eval/run` 的 baseline 也已改为显式提升，不再用上一轮结果覆盖。

## 可选 Langfuse

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-observability.txt
```

使用 `langfuse==5.0.4`，Python 至少 3.11，Langfuse 自托管服务至少 4.59.0；本次参考源码为 4.70.0。设置 `ECHOMIND_TELEMETRY_ENABLED=true`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_BASE_URL` 后启用。

完整请求包含 memory、intent、routing、Agent、LLM、tool、retrieval/rerank 子观测。场景通过 run_id/case_id 和任务评分关联。实验报告及配对比较仍由 EchoMind 保存；当前接入重点是观测与评分导出，不会自动将本地数据集同步成 Langfuse hosted dataset。

默认不导出正文，用户/会话标识使用 HMAC；设置稳定的 `ECHOMIND_TELEMETRY_HASH_SALT` 以便跨进程关联。只有 `ECHOMIND_TRACE_CONTENT=true` 才允许正文导出，直接标识和凭据仍脱敏。SDK 缺失、导出或 flush 异常均不影响客服结果。应用退出会 shutdown exporter，短命脚本应 flush。

## 上下文与检索策略

知识导入兼容旧 title/content，也可传 id、version、source、scope、effective_at。返回 ContextRef 与稳定证据标识。版本/生效时间/作用域用于选择上下文，scope 字段本身不构成生产 ACL；可信身份与强制授权需独立实现。

画像更新保留 added/updated/deleted、来源会话、版本与推断标记。`get_profile_audit(user_id)` 只读取当前空间对应用户的审计；`seed_profile` 仅允许唯一评测空间，线上默认空间不能用它初始化。

检索开关：

```dotenv
ECHOMIND_QUERY_REWRITE_POLICY=always
ECHOMIND_RERANK_POLICY=always
ECHOMIND_RETRIEVAL_TIMEOUT_S=30
```

`always` 保持兼容行为，`never` 关闭对应步骤，`adaptive` 使用可解释规则按需执行。策略快照与动态计数分别记录，实际重排失败会显示回退而非标记为重排成功。先在相同数据、模型、知识和裁判上比较策略，再选择默认值。

## 已知验证边界与后续

本轮离线验证不能证明真实模型准确率、生产延迟或迁移收益。真实 Redis/Chroma、真实 Langfuse、模型回放及业务专家审定分别需要后续联调记录；生产鉴权和多租户 ACL 不在本轮内。

当前知识版本选择会读取候选全集后筛选，适合演示和小规模回归；大型知识库需要版本清单或检索端过滤后再验证容量与性能。

OpenViking 后续先部署 server-only 侧车、使用脱敏知识和记忆副本双读，比较召回、任务成功、记忆差异、总 Token 和延迟；确认收益后再评估迁移。当前采用其上下文来源、分层评测和记忆差异思路，保留现有存储。引用代码前需核对具体组件及当前许可证。
