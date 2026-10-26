# 项目来源与整理范围

本仓库基于工作区中的 EchoMind Python 和 Vue 项目快照整理。原始仓库信息由本地 Git remote 与提交记录核验：

| 部分 | 原始仓库 | 对应快照提交 |
| --- | --- | --- |
| Python 后端 | [Biscuit-AI531/EchoMind](https://github.com/Biscuit-AI531/EchoMind) | `5d865b7` |
| Vue 前端 | [Biscuit-AI531/EchoMindFrontend](https://github.com/Biscuit-AI531/EchoMindFrontend) | `a95a6c8` |

导出包含工作区已有的前端调整，不代表上述提交未经修改的逐字副本。

本次整理于 2026-10-26，范围包括：

- 合并为 `backend/` 与 `frontend/`，前端统一使用 Python 服务。
- 补齐项目首页、运行指南、环境变量模板和 CI 构建配置。
- 容器的应用数据与日志改用命名卷，并预建评测目录，避免首次启动的目录权限问题。
- 排除模型凭据、运行数据库、日志、依赖缓存、本地学习与简历资料。
- 采用新的 Git 历史，避免带入原仓库曾跟踪的敏感配置与数据库。

多 Agent、RAG、记忆、监控等核心实现来自原有项目；本次整理没有将这些能力宣称为新的独立原创实现，也没有添加未经验证的效果指标。

后续本地升级（2026-10-26，`codex/scenario-evaluation-upgrade`）在上述发布快照上修复工具轨迹、实体传递、评测入口一致性与基线行为；新增 ChatService、隔离回放、合成场景、实验比较、可选观测适配和上下文来源治理。参考 Langfuse `0e024ca` 与 OpenViking `009c483` 的公开设计和评测方法，保留原项目来源；合成数据及离线测试不代表生产效果，第三方组件授权分别适用。

原始快照中未发现 LICENSE 文件，因此本仓库没有擅自指定 MIT、Apache-2.0 等许可证。原代码的权利归原权利人，公开分发或商业使用前应确认相应授权。
