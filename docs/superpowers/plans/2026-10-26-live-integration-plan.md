# EchoMind Live Integration Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans / subagent-driven-development. Keep checkboxes tied to actual evidence. Never report a mock or SDK-only check as a real platform integration.

**Goal:** 在上一阶段174项离线测试基础上验证真实模型、Chroma/Redis及Langfuse边界，修正联调暴露的问题，并形成可重复的完整联调入口。

**Architecture:** 默认只做安全预检；显式指定阶段才调用模型或服务。所有数据为合成场景，存储使用唯一 `eval_<uuid>`；真实服务、测试替代、SDK协议和人工审核分别记录。准备独立 Compose 项目，先存储再观测，系统安装不混入项目脚本。

**Tech Stack:** 当前项目Python环境、Anthropic 0.40.0、Chroma 0.5.23、Redis 5.2.1客户端、Langfuse 5.0.4；可选Docker Compose。

**Spec:** [已实施设计](../specs/2026-10-26-scenario-evaluation-design.md) 与用户“继续下一步”的真实联调请求。

## Global Constraints

- 不输出/归档API Key、Redis密码、Langfuse Secret；已有模型配置只在进程中读取，不复制进Git。
- 默认预检不调用付费模型。模型协议先单请求、小输出；场景回放先最多3条，固定Judge并记录实际调用数。
- 只使用合成用户、知识和问题，清理仅本次eval命名空间；不连接原始业务集合读写。
- 未提供的Redis/Langfuse标记blocked；fakeredis只能是显式partial模式，不能自动替代真实验收。
- 上一阶段只准备系统环境；用户再次要求“继续下一步”后已尝试正常UAC启用WSL与per-user Docker安装，结果见windows-runtime-setup.md。协议接受和系统重启仍由用户操作。
- 业务标注报告为AI技术预审，不能宣称已经获得业务专家确认。
- 保留当前未提交升级代码，不push、不部署到外部环境。

## Environment snapshot

- 本地发布后端尚无.env；原Python项目有DeepSeek模型配置，可在进程中按明确路径读取。
- localhost的Redis、Chroma、Langfuse端口当前均不可用。
- 未发现可用Docker CLI；WSL命令提示子系统未安装。旧 `.dev-logs/fake_redis.py` 为fakeredis。
- 推荐完整联调采用本机Docker Compose；当前先完成不依赖Docker的验证和部署准备。

## Review Focus

- SDK错误不能被Noop/持久化fallback掩盖为联调成功。
- Python本地依赖/embedding模型不同于真实服务语义，报告必须标明。
- 真实LLM延迟、工具循环、Judge无效输出和超时均保留原始状态。
- 关键标签不能为迁就模型/规则错误而改标；确定的实现缺陷用RED→GREEN修正。
- 服务缺失不能提前标记总体验收完成，后续文档提供准确恢复入口。

## Task 1: 环境预检与脱敏报告

Files: `backend/evaluation/integration.py`, `backend/tests/test_integration.py`。

- [x] 先测试配置路径、凭据脱敏、默认无模型调用、未连接服务时blocked而非passed。
- [x] 实现分阶段CLI和结构化报告，记录包版本、配置存在性、服务身份与证据类型。
- [x] 验证默认命令不会发送模型请求或改动现有存储。

## Task 2: 真实模型与SDK边界

- [x] 安装固定的真实Chroma/Langfuse SDK到隔离环境；Python3.12缺MSVC，改用已有Python3.11预编译包，版本不变。
- [x] DeepSeek Anthropic兼容接口单请求验证，64输出Token上限；归档输入88/输出27、约4156ms。
- [x] 真实Langfuse SDK+OTel内存exporter及score MockTransport测试；仅记SDK/协议，不记平台通过。
- [x] 对日期字段、真实Anthropic内容块兼容缺陷先RED后修复；补充默认隐私与任意对象拒绝测试。

## Task 3: 存储与小样本链路

- [x] 真实本地Chroma跨进程持久化、过滤查询、用户隔离与清理验证；显式测试向量，不计默认embedding验收。
- [x] 原生Chroma HTTP显式向量回环补测；原始总failed记录保留，额外清理GET状态码断言错误与真实DELETE/SQLite清理证据分开说明，见chroma-http-check.md。
- [x] 真实Docker Redis验证连接、TTL、读写、隔离和清理；来源由项目、端口和固定镜像确认。
- [x] 默认ONNX缓存校验及真实Chroma中文文本HTTP回环通过；真实Langfuse trace/score及3个case读回通过。
- [x] 同3条合成场景回放，首轮1通过/2 invalid保留；修复结构化thinking预算后3/3通过，归档真实用量与清理结果，不提升baseline。
- [x] 如仅完成部分真实组件，报告明确标为partial，不能提升为完整联调基线；所有阶段报告均 full_integration_verified=false。

## Task 4: 可重复Docker环境

Files: `backend/compose.integration.yml`, `backend/.env.integration.example`, `docs/integration.md`。

- [x] 独立Compose project、仅localhost端口、固定镜像digest、健康检查、独立数据卷。
- [x] 配置Redis/Chroma先启动；Langfuse六组件用可选profile，原部署配置保持兼容。当前未实际启动容器。
- [x] 说明密钥本地生成、启动/验证/正常停止命令；不默认删除数据卷。
- [x] 无Docker时只记录配置准备、manifest核对、PyYAML解析及静态验证，不声称容器已运行。

## Task 5: 场景预审与问题修正

- [x] 技术预审全部120条，区分实现缺陷、标注歧义、待业务确认。
- [x] 修复金额500误路由、升级回复虚假跟进承诺与沟通线索丢失；歧义样本和不可能断言保留待审，不修改标签迁就实现。
- [x] 生成dataset-review.md，记录样本ID、证据和审核状态；业务审核未完成。

## Task 6: 复核与交付

- [x] 重启后最终完整Python3.11联调环境257 passed in53.22s；默认场景校验120条/135轮/702断言。前端本阶段未改，沿用上阶段3测试及构建证据。
- [x] 真实/partial/blocked逐项列证据、失败原因、恢复命令与下一次验收标准。
- [x] 更新原执行记录；尚未达到完整真实链路标准，因此未提升基线。

## 系统动作与后置范围

- [x] 用户已管理员启用WSL并重启；Docker安装成功、8服务healthy，未由代理接受协议或重启系统。见windows-runtime-setup.md及live-integration-results.md。
- [ ] 注册外部Langfuse账户或接受服务协议。
- [ ] 没有实测收益证据的OpenViking迁移。

执行证据及剩余环境阻塞见 [本阶段记录](2026-10-26-live-integration-progress.md)。

重启后真实验收见[最终结果](2026-10-26-live-integration-results.md)。剩余范围是120条业务审核/全量回放、可选app镜像构建及生产/性能验收，不能将3条smoke扩大解释。
