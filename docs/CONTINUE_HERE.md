# 从这里继续 EchoMind

最后交接：2026-10-26（Asia/Shanghai）。用户要求今天停止开发，把当前代码和执行文档推到 GitHub，以便在另一台电脑接着做。

## 获取正确版本

仓库：`https://github.com/kuixinma-ui/echomind.git`（私有）。工作分支为 `codex/scenario-evaluation-upgrade`；本次同时将验证后的提交快进到 `main`，默认clone/pull可取得交接文档。实际远端提交以GitHub最新记录为准。

```bash
git clone https://github.com/kuixinma-ui/echomind.git
cd echomind
```

已有副本：`git fetch origin`，保存本机其他未提交工作后再快进相应分支；不要用reset/clean覆盖本机工作。

## 当前真实状态

- 原基础升级已经完成：共享ChatService、隔离运行时、120条场景、严格Judge、版本/范围知识与记忆证据、冻结基线/配对报告、可选Langfuse、Vue评测界面。
- 上一阶段真实端到端smoke已验证3/3通过：问候、知识发票、跨会话安卓登录；真实Redis/Chroma默认embedding/Langfuse trace/score读回都已通过。首次1通过/2 invalid报告也保留，修复是显式结构化thinking策略和安全JSON解析。
- 当前收工验证：**后端301 passed in14.93s**；前端3项测试通过，Vite build通过。包含本轮已完成但尚未真实回放的新候选/账号规则/预算功能。
- 本轮新增：v1.1候选120条/135轮/706断言；账号副词误判修复；第4个账号Skill及Billing条件职责；SDK调用预算和模型deadline、报告中断保护。
- 另从Git已提交树导出了不含本机忽略文件的干净副本，再次执行后端测试：301通过、0错误/失败/跳过，验证代码不依赖旧机data或临时脚本。
- **本轮没有启动120条候选真实回放**，没有声称候选全通过或业务已审，没有promote baseline。
- 用户要求停止后，旧电脑8个echomind-integration容器已正常stop，卷保留。新电脑没有这些容器/卷，需自行创建独立环境。

## 必读文档

1. [当前执行计划](superpowers/plans/2026-10-26-candidate-full-evaluation.md)：下一阶段，T1–T3已完成，预算初版已完成并离线验证，分批驱动/全量回放未完成。
2. [v1.1候选变更](superpowers/plans/2026-10-26-dataset-candidate-changes.md)：仅6条实质变化；AI演示口径不等于业务批准。
3. [120条技术预审](superpowers/plans/2026-10-26-dataset-review.md)：保留歧义与能力边界，不应追分改标。
4. [真实联调结果](superpowers/plans/2026-10-26-live-integration-results.md)：上一阶段已验证证据，不是当前候选全量成绩。
5. [联调指南](integration.md)、[评测用法](evaluation.md)、[平台API准备](superpowers/plans/2026-10-26-langfuse-readback-prep.md)。

## 另一台电脑：先做不付费验证

推荐Python3.11（原Windows的Chroma0.5.23在3.12缺MSVC，3.11有对应轮子）、Node20.19+或22.12+、Docker Compose。无需重做旧机WSL安装；只检查新机自身条件。虚拟环境、Docker、安装包和模型缓存不通过Git迁移。

Windows PowerShell，在仓库根目录：

```powershell
$env:PYTHONUTF8='1'
py -3.11 -m venv .venv-integration
.\.venv-integration\Scripts\python.exe -m pip install -r backend/requirements.txt -r backend/requirements-test.txt -r backend/requirements-observability.txt
$env:PYTHONPATH='backend'
.\.venv-integration\Scripts\python.exe -m pytest backend/tests -q
.\.venv-integration\Scripts\python.exe -m evaluation.scenario_runner --mode validate --dataset backend/evaluation/datasets/customer_service_v1_1_candidate.json
cd frontend
npm ci
npm test
npm run build
```

Linux/macOS：用 `python3.11 -m venv .venv-integration` 和 `.venv-integration/bin/python`；在仓库根执行 `PYTHONPATH=backend .venv-integration/bin/python -m pytest backend/tests -q`。生产依赖仍以requirements为准；安装平台差异如实记录，不擅自升级Chroma主版本。

## 新电脑创建本地服务配置

下面脚本只生成随机本地服务秘密和headless测试项目ID/key，不联网、不输出秘密、不覆盖已有文件：

```powershell
# 仓库根目录
.\.venv-integration\Scripts\python.exe backend/scripts/prepare_integration.py
```

生成的 `backend/.env.integration` 被Git忽略。模型API Key没有迁移：在新机该文件配置自己的 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` / 固定的Judge模型。DeepSeek Anthropic兼容URL为 `https://api.deepseek.com/anthropic`；支持该扩展时显式选 `ECHOMIND_STRUCTURED_THINKING=disabled`，普通Agent生成保持原配置。

有出站代理时按新机实际设置HTTP(S)_PROXY；不要照搬旧机7990端口。NO_PROXY应包括本机和Compose服务，避免Windows系统代理干扰localhost。仅在Langfuse已就绪且要导出时设置 `ECHOMIND_TELEMETRY_ENABLED=true`，正文默认false。

```powershell
cd backend
docker compose --env-file .env.integration -f compose.integration.yml -f compose.langfuse-headless.yml --profile langfuse config --quiet
docker compose --env-file .env.integration -f compose.integration.yml -f compose.langfuse-headless.yml --profile langfuse up -d --wait
```

先执行 `evaluation.integration --stage storage --env-file .env.integration`；再执行 `--stage langfuse`。在backend下使用 `..\.venv-integration\Scripts\python.exe -m ...`。它们0个LLM调用；平台探针有真实合成写入和读回。headless不创建UI用户，也不需要外部账户。

Chroma默认embedding位于客户端，新机首次真实文本导入可能下载约83MB官方模型；缓存未迁移。显式测试向量通过不代替默认embedding验收。

## 准确的下一步

1. 先读预算实现与测试，确认当前完成状态。`observability/budgets.py` + instrumentation + ScenarioRunner已限制运行内SDK create尝试；Judge/画像也计入。模型deadline用于provider等待/新case开始，初始化及清理不在其硬保证内；SDK内部HTTP重试不计为额外SDK调用，不能称货币硬上限。
2. **尚需处理code fingerprint的运行目录噪声**：当前 `evaluation/scenarios.py::_tree_snapshot(backend,'*.py')` 未排除backend/data中的本地驱动.py。实现分批manifest前，应只对code快照排除运行产物，保留skills资源快照完整性；补回归测试。今天没有为此继续改代码。
3. 完成可恢复分批驱动与汇总：8组各15条、最多2组并行、每组最多200次SDK create/模型阶段1200秒；保存完整数据/代码/模型/Judge/skills/retrieval指纹。已完成组不自动重新收费回放，配置变更开新批次。
4. 固定代码与配置后做候选首组，再按真实结果完成8组；所有120样本保留分母，区分passed/failed/invalid/timeout/error/hard_fail/预算中断，并验证eval UUID清理及平台评分。
5. 保留失败证据，分析实现问题、模型问题与oracle歧义。`cross_session_memory_13`等隐私保存偏好/召回边界仍待明确；候选不能据通过率标记业务已审。未经明确动作不promote baseline。

示例单组调用（完成前述准备后才显式运行，会收费）：

```powershell
# backend目录，先在进程加载新机.env.integration
..\.venv-integration\Scripts\python.exe -c "from dotenv import load_dotenv; load_dotenv('.env.integration', override=True); from evaluation.scenario_runner import main; raise SystemExit(main())" --mode live --dataset evaluation/datasets/customer_service_v1_1_candidate.json --scenario basic_routing --limit 15 --judge --max-model-calls 200 --max-model-seconds 1200
```

## 已迁移与未迁移

代码、候选集、执行文档、测试与经过检查的合成证据快照随Git迁移。`docs/evidence/2026-10-26/`中的JSON为历史只读证据，不会被自动载为baseline。

真实模型密钥、`.env.integration`、本机Docker卷/数据库、虚拟环境、node_modules、安装器、ONNX缓存、原始机器日志没有提交。根据上述模板重新配置即可继续，不应寻找或复制旧机秘密。

参考仓库未嵌入本项目：Langfuse commit `0e024ca8eae1da68f08514a46edb7a37a4f2d0b2`；OpenViking commit `009c483f20806d63979cec0f3a7fb1304fd600df`。官方链接与来源见[provenance](provenance.md)，只有后续研究需要时再按这些版本获取。
