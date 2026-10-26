# Execution ledger — plan: 2026-10-26-scenario-evaluation-upgrade.md

- 基线：`82c3b3a`；工作分支：`codex/scenario-evaluation-upgrade`。原始资料和 reference-repos 不改。
- 授权：用户明确要求根据上一轮升级方案编写执行计划并逐步执行，无需重复等待计划确认。
- 决策：在干净的发布仓库新建本地分支直接实施，避免引入另一个用户难以定位的副本；最终不 push、不部署。
- 决策：按独立文件边界分工，集成和最终审查由主执行者完成；出现共享文件时顺序修改。
- 预检：Task1→3 的 AgentResult 字段保持；Task3→2/5 的 chat() 统一返回 dict；Task4→5 的工厂隔离；Task6 可选且默认Noop；Task7→5/8 元数据向后兼容。
- 当前：计划已创建，正在准备隔离测试环境和运行基线。

## 阶段证据

- 基线：原有 Agent 测试 9 passed。
- Task1：22 passed；见 trace-task-report.md，正常/失败/并发/降级轨迹和实体传递已覆盖。
- Task2：37 passed；见 evaluator-task-report.md，严格评分、冻结基线与报告归档。
- Task3/4：初次服务与隔离专项 8 passed；旧评测接口切换隔离后 6 passed。
- Task5：25 passed（含报告测试）；初版 120 cases / 135 turns / 687 assertions。审查修复后为 702 assertions。
- Task6：遥测边界 26 passed；运行时 instrumentation 最终 33 passed；实验关联及调用链集成专项通过。
- Task7：28 passed；见 context-task-report.md，额外独立 RED 修正了 Chroma where 双键和压缩顺序。
- 首次整合：156 passed；前端请求契约 3 passed，Vite build 通过。
- 独立最终审查：2 Important + 1 Minor，无 Critical；明细保存在 backend/data/review/review-findings.md。
- 修复中：异步工厂拥有构造中的资源并回滚；跨会话种子显式归档、Judge携带初始事实，新增 memory_recalled 硬断言及移除记忆的负对照；校验卡读取 case_count。
- 跨会话修复 RED：3 failed；GREEN：相关场景专项24 passed，随后服务/API/报告等整合专项37 passed。
- 决策：create_runtime 改为 async，调用点统一 await，避免同步构造失败丢失资源；新增接口本轮尚未发布，不影响已有HTTP契约。
- 决策：可比较条件允许显式 vary=code/model/prompt/skills/retrieval；dataset/knowledge/judge仍固定，避免把受控实验误判为全部不可比。
- 决策：合成数据只作回归夹具，业务确认和真实服务联调不计为已验证；Langfuse本轮导出观测和评分，不自动同步hosted dataset。

## 最终验收（2026-10-26）

- Task1–Task9：本轮代码、数据、界面、测试、文档完成，计划主体步骤已勾选；可选外部阶段仍未勾选。
- Important 1 已修复：archive_session 显式持久化历史，memory_recalled 硬断言检查实际证据；Judge接收具体初始事实。tests/test_cross_session_setup.py 包含删除记忆后的负对照，相关专项24 passed。
- Important 2 已修复：异步初始化逐步拥有资源，失败及取消均回滚；partial memory/knowledge、policy、orchestrator初始化和清理异常均有测试，专项37 passed。
- Minor 校验数量显示已修复并实际浏览器确认“120 条数据”；分组显示中文。
- 最终完整后端：174 passed in 23.56s，无 skipped。命令：backend 下 ../.venv/Scripts/python.exe -m pytest tests -q。
- 最终数据校验：120 cases、135 turns、702 assertions、8 scenarios、19 intents；dataset_status=synthetic_pending_review。
- 前端：3 passed；npm run build 成功（Vite 7.3.5，15 modules transformed）。
- git diff --check：退出码0；只有本机CRLF/LF配置提示，无空白错误。
- UI：本机无模型评测模式已点验目录、默认validate、120条结果、历史；截图 backend/data/review/evaluation-workbench.png。
- 已明确的范围判断：未运行真实provider、Redis/Chroma、Langfuse集成；未验证生产准确率/时延；未审定业务政策；OpenViking迁移、生产认证/ACL明确后置。
- 本轮Langfuse提供观测与评分关联，本地实验和配对报告由EchoMind保存，hosted dataset同步不在本轮内。
- 性能边界：知识多版本过滤当前读取候选全集，小规模演示可用，大库需manifest/检索端过滤后另做性能验收。
- 交付：本地 codex/scenario-evaluation-upgrade 分支，变更未提交、未推送、未部署。保留代码与计划供用户审阅。
- 验证用临时页面和两个本机预览进程已关闭；截图保留。前端也展示逐轮 error_detail，便于定位超时/执行失败。

## 后续真实联调阶段（2026-10-26）

用户要求“继续下一步”后，已进入真实模型/SDK/存储边界验证及120条场景技术预审。新阶段最终 **212项后端测试通过**；DeepSeek单请求、真实本地Chroma跨进程持久化、Langfuse SDK本地导出协议已验证。修复500金额误路由、升级跟进承诺、沟通线索丢失、SDK序列化、Chroma/PostHog依赖及严格存储模式；业务标签仍待确认。

本机暂无Docker/真实Redis/Langfuse平台，新阶段仍为partial，未提升baseline。具体证据、原生Chroma HTTP补测结果与恢复入口以 [真实联调执行记录](2026-10-26-live-integration-progress.md) 为准，[联调指南](../../integration.md) 提供独立Compose配置和逐步命令。
