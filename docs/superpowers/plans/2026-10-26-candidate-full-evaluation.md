# Candidate Dataset and Full Evaluation Implementation Plan

> **For agentic workers:** Use subagent-driven-development / executing-plans. Keep original evidence, use TDD for behavior changes, verify actual outcomes before marking steps complete.

**Goal:** 把3条smoke推进到120条候选场景回放，修复可确定的场景/能力缺口，报告真实失败与剩余业务问题。

**Architecture:** 保留v1.0，新增v1.1候选；不将AI规则决定写成业务审批。现有共享ChatService/隔离存储/Langfuse不变。按8组独立运行，最多两组并行；通过ContextVar共享运行预算限制SDK create尝试和模型等待，保留全部样本分母与清理证据。

**Tech Stack:** 现有Python3.11联调环境、Anthropic0.40、Redis/Chroma、Langfuse4.70/SDK5.0.4；前端本阶段不改。

**Spec / rationale:** [已有升级设计](../specs/2026-10-26-scenario-evaluation-design.md)、[120条技术预审](2026-10-26-dataset-review.md)、[真实smoke结果](2026-10-26-live-integration-results.md)。用户明确要求“下一步该干什么，继续”，因此按可撤回候选和现有架构推进。

## Global Constraints

- 原v1.0、既有报告和baseline保留，不追求通过率改标，不自动promote。
- 候选status保持synthetic_candidate_pending_business_review；执行者提出的演示口径不等于用户/业务专家批准。
- 演示口径：普通资料变更account；安全事件/明确安全设置account_security；人工政策咨询query，实际要求转接human_handoff。
- 不删真实检索、来源、跨会话证据和hard安全断言；仅修正矛盾oracle或明确原本中性表达。
- 只读取本地模型配置到进程，不复制原模型密钥，不输出服务秘密；仅合成数据和eval UUID。
- 首轮每组最多200次SDK create、模型阶段1200秒，最多2组同时运行。SDK内部HTTP重试不是计数单位，不声称货币费用硬上限；初始化/存储清理不在模型等待deadline保证内。
- 预算耗尽后停止新模型调用，后续样本保留分母并记明确错误；被预算中断的报告不能promote或当作完整受控比较。
- 组报告不自动重跑以追逐分数；续跑只恢复未完成组，指纹不一致则创建新批次。
- 不提交、推送、外部部署或迁移OpenViking；8个本地依赖保持隔离。

## Review Focus

- 弱“安全”副词与真实被盗/异常登录是否仍正确分开。
- 账号Skill是否按billing匹配且未污染正常资金核验。
- 候选变更是否都有可审查理由，所有原hard边界是否仍存在。
- 组件catch/fallback是否吞掉预算中断并将样本误判passed。
- 并行/子任务预算是否共享，拒绝的调用是否被误计为已发出。
- 汇总是否遗漏失败/invalid或把候选分数解释为生产准确率。

## Tasks

- [x] T1：意图副词误判，先RED再最小规则修复，保持明确安全事件。
- [x] T2：新增账号Skill和Billing条件职责；先RED再验证资金流程保留。
- [x] T3：v1.1候选+逐项变更记录，120条/135轮/706断言/8组/原知识；v1.0不改。
- [x] T4：预算初版与CLI已实现：reserve前拒绝、模型deadline、并行共享、错误不吞、预算中断报告不可promote/compare；新测试10通过、关联46通过。尚需执行前审查。
- [x] T5：收工完整后端301 passed in14.93s、前端3测试及Vite构建通过；候选校验通过。运行指纹冻结与批次manifest尚未执行。
- [ ] T6：可恢复的分组真实回放，先检查依赖，归档8组和总体覆盖/状态/用量；不假定全通过。
- [ ] T7：核对Langfuse观测及UUID清理，分析真实失败，更新执行记录和下一步清单。

## Files

- intent、skills/Agent角色、候选数据与各自测试由独立文件边界实现。
- root：`observability/budgets.py`、instrumentation、scenario_runner、report_store及预算测试；本地批次驱动和manifest放Git忽略的data/integration。
- 报告：各agent任务记录与本计划完成记录；全量执行后创建candidate-full-results.md。

## 收工交接

用户2026-10-26明确要求停止今天开发并提交推送到GitHub，以便另一台电脑继续。因此T6/T7未开始，不再新增模型调用；本次8个联调容器正常stop，卷保留。交接入口 [CONTINUE_HERE](../../CONTINUE_HERE.md)。

提交前发现的后续事项：code快照当前会包含backend/data中的本地.py驱动；分批manifest前应仅在code快照排除运行产物，并测试skills资源不会被误排除。今天未继续修复。原v1.0、历史smoke/失败报告及baseline均保留。

用户本次明确授权提交推送，覆盖前述“暂不提交/推送”的阶段限制。先保存工作分支，再仅在main仍为原基线且可快进时同步main；不强推、不覆盖远端其他工作。
