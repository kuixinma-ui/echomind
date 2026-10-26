# 首次真实模型回放：三条样例选择与执行边界

日期：2026-10-26。此文档来自代码与合成数据的只读核对；未读取 `.env`，未调用模型或真实服务。样例仍为 `synthetic_pending_review`，不提升baseline。

## 建议的三条样例

| 顺序 / case_id | 用户输入与初始状态 | 预期与可观察证据 | Judge |
| --- | --- | --- | --- |
| 1. `basic_routing_01` | “早上好，我刚来到这个客服窗口。”；无历史 | intent=greeting，primary_agent=general，非空回答，整轮≤120000ms。无工具断言，只验证首轮链路，不把问候成功视为业务正确率 | 无semantic断言，本条不必调用Judge |
| 2. `knowledge_boundaries_04` | “请按演示政策解释企业电子发票需要哪些资料。”；无历史 | intent=invoice，primary_agent=billing；成功调用 `search_knowledge_base`，实际trace的document_ids包含invoice；回复应按演示政策说明企业抬头/税号、缺项与未真实开票边界 | 必需；1条required、hard semantic，证据invoice+demo_scope。未启用Judge应invalid，不能改记通过 |
| 3. `cross_session_memory_01` | prior会话：“我使用安卓14手机。”；当前：“我之前说过只用安卓手机，请按我的设备给登录401的排查步骤。” | intent=technical_login，primary_agent=technical；当前回话须有prior归档的memory_evidence_id（hard）；语义检查利用历史安卓14，不重复索要已知系统版本。未强制知识工具，因此与第2条分工不同 | 必需；1条required、非hard semantic，证据technical+demo_scope。hard=false仍影响case通过 |

选择理由：三个意图分属general/billing/technical，不碰account/account_security分类争议、固定升级节点强制检索的 `knowledge_boundaries_11`，也不将“演示转人工”误作真实外部业务动作。

跨会话样例仍需真实Redis、Chroma默认embedding及显式 `archive_session`；只验证当前回复提到“安卓”不够，必须检查prior归档证据和安卓14历史事实。知识工具样例的“真实”是检索实际合成快照，不是创建实际发票。

## 必须先修复的scope路径

已确认当前固定9篇知识均为 `scope=evaluation`，而 `KnowledgeBase.search()` 默认 `scope=public`。`build_shared_rag_tools → search_with_rewrite → knowledge.search_handler` 未传受控scope，导致真实检索过滤掉全部评测文档。

使用实际KnowledgeBase及纯内存collection复核：导入9篇知识后，默认scope返回0条，显式evaluation返回5条。该复核没有模型/服务调用。不得通过改数据scope或标签来绕过。

经主执行者授权，修复限定于runtime注册知识handler：eval UUID运行时固定evaluation，在线固定public，调用者参数或context不能扩大范围。修复与RED→GREEN证据将在本文末补充。

## CLI能否精确选择

当前只有可重复 `--scenario` 和 `--limit`，先按原文件顺序筛组选组、再截断。三个 `--scenario` 加 `--limit 3` 会选basic_routing前三条，**不能精确运行上表三条**。没有 `--case-id` 参数。

最小现有入口：在Git忽略的 `backend/data/integration/` 生成衍生三条子数据集，保留原knowledge、case内容与状态，仅按上述ID选取cases；先用 `validate_dataset` 验证，再执行：

```powershell
# 在backend目录；先确认配置只连接独立测试存储，模型/Judge及检索策略已固定。
..\.venv-integration\Scripts\python.exe -m evaluation.scenario_runner --mode validate --dataset data/integration/customer_service_smoke_v1.json
..\.venv-integration\Scripts\python.exe -m evaluation.scenario_runner --mode live --dataset data/integration/customer_service_smoke_v1.json --limit 3 --judge
```

子数据集fingerprint与120条全量不同；比较和基线不能当成同一coverage。本文不生成该文件、不发起回放，也不扩展CLI。若后续增加repeatable `--case-id`，应校验未知/重复ID，按明确顺序选择并保留实际子集fingerprint，不能默默忽略无效ID。

## 调用数量与超时预算

三条均是1轮：每轮 `turn_timeout_s=120`、`max_latency_ms=120000`；两条semantic各 `judge_timeout_s=60`。在初始化、归档、清理及取消及时返回这一前提下，执行与Judge等待的名义合计为480秒。初始化、知识embedding导入、跨会话种子归档和清理没有统一run deadline，故**480秒不是总墙钟硬上限**。

正常单Agent、无重试/降级、一次工具检索的估计：

| 样例 | 逻辑SDK调用估计 | 说明 |
| --- | --- | --- |
| 问候 | 3 | 意图1、直接回答1、画像提取1；没有semantic Judge |
| 发票检索 | 6–7 | 意图1、发起工具+最终回答2、查询改写1、画像1、Judge1；候选数超过top_k时再重排1 |
| 跨会话登录 | 4起 | 意图1、回答1、画像1、Judge1；额外知识/诊断工具循环会增加调用。prior归档不调用LLM，但需要Chroma embedding |

以上是正常路径估计，**不是费用或调用次数上限**：

- Agent生成最多3轮；专属Agent失败可再走General最多3轮。每条模型响应里的tool_use块数没有显式硬上限，每次知识检索可能增加rewrite/rerank。
- Anthropic 0.40.0安装源码默认 `max_retries=2`、HTTP默认timeout600秒；应用客户端未全局覆盖。一次SDK create可能包含最多3次HTTP尝试，外围turn/Judge timeout会在更早时尝试取消。
- 输出max_tokens分别包括意图256、General900、Billing1100、Technical1200、画像512、Judge256、rewrite/rerank各256；它们是单调用输出上限，没有run级输入/输出Token或货币上限。
- `usage.model_calls` 在SDK create入口计尝试，Token仅累加provider返回的usage；SDK内部HTTP重试不额外计数。不能从这个计数声称精确HTTP请求数或失败请求费用。
- 若需要硬预算，应由后续实现统一的调用上限/明确重试策略/整体deadline；本文不擅自增加。首次可显式选rewrite/rerank=never减少链路调用，但必须记录策略fingerprint，与默认always实验分别归档。

执行时必须保留每条actual回答、tool_traces、memory_evidence_ids、profile_status、usage、evaluation_overhead_usage及cleanup状态；无效Judge、超时、缺证据、清理失败不转换为通过。

## scope修复的RED → GREEN记录

经主执行者授权，仅修复 `services/runtime.py` 注册的知识handler。评测eval UUID固定scope=evaluation；在线scope=public。参数中的scope无论是缺省、evaluation还是internal，均由运行时边界覆盖；context中的internal提示也不能扩大范围，原params不被修改。数据scope与标签未改。

新增 `tests/test_runtime_knowledge_scope.py` 使用实际KnowledgeBase、实际search_handler及共享RAG工具路径。只有Chroma持久化/模型/记忆边界使用离线替身，不将这些结果当真实服务证明。三个知识字面夹具分别为public、evaluation、internal；验证在线只见public，eval可见public+evaluation，均不能见internal。

- RED：新增6种边界组合，**5 failed / 1 passed**，分别复现评测文档缺失与参数绕过边界。
- GREEN：scope专项与runtime/context_storage关联三文件 **30 passed in 2.35s**。
- 完整回归：Python3.11 `.venv-integration` 下 **218 passed in 16.43s**，无skip。实际命令为 `python -m pytest backend/tests -q --junitxml backend/data/integration/pytest-scope-fix.xml`；同一JUnit结果复制到主执行者指定的 `backend/data/integration/pytest-python311.xml`，未重复运行。没有发起任何真实模型/外部服务调用。
