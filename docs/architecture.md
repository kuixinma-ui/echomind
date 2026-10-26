# 架构与代码阅读路线

## 对话链路

`api/main.py` 的 lifespan 初始化模型客户端、记忆、知识库、工具管理、Agent、Skills、监控和评测器。`POST /chat` 接收用户消息与会话标识，读取历史记忆并识别意图，然后交给编排器路由。Agent 在执行中按需调用知识检索工具，最后合并回复、写回记忆并返回结构化结果。

| 模块 | 主要职责 | 入口 |
| --- | --- | --- |
| API | 组件生命周期与对话、知识、轨迹接口 | [main.py](../backend/api/main.py) |
| 意图 | LLM、向量分支与模式匹配融合 | [intent_recognizer.py](../backend/core/intent_recognizer.py) |
| 编排 | 专业 Agent、复合问题、路由与回复合并 | [agent_orchestrator.py](../backend/agents/agent_orchestrator.py) |
| Agent 工具 | 工具范围、输入与执行逻辑 | [tools.py](../backend/agents/tools.py) |
| RAG 工具 | 改写、并行搜索、缓存、超时、熔断、重排 | [tool_manager.py](../backend/mcp/tool_manager.py) |
| 知识库 | 文本切片与 ChromaDB 导入、检索 | [knowledge_base.py](../backend/mcp/knowledge_base.py) |
| 记忆 | Redis 工作记忆、ChromaDB 历史与画像 | [conversation_memory.py](../backend/memory/conversation_memory.py) |
| Skills | 读取业务规则并注入提示 | [skill_loader.py](../backend/core/skill_loader.py) |
| 监控 | 指标采样、异常检测与路由惩罚反馈 | [performance_monitor.py](../backend/monitor/performance_monitor.py) |
| 评测 | 意图指标、LLM Judge、基线与回归 | [evaluator.py](../backend/evaluation/evaluator.py) |

## Agent 与工具

General 负责通用分流，Technical 负责技术排障，Billing 负责账单字段核对，Escalation 生成人工交接摘要。各 Agent 具有不同的角色规范与工具集合；共享知识检索工具由编排器注入。模型返回 `tool_use` 后，执行层按白名单验证工具与参数，将 `tool_result` 交回模型。

前端使用 `/chat` 返回的 `request_id` 查询 `/trace/tool/{request_id}`，展示工具名称、输入与耗时。此轨迹为当前实例的运行数据，不随源码发布。

## 数据与依赖

Redis 保存短期工作记忆；ChromaDB 保存知识片段、历史记忆和用户画像。Compose 同时定义了独立 Chroma 服务，而实现也支持本地持久化回退，实际使用方式取决于连接状态与配置。镜像会预下载 Chroma 默认 ONNX embedding 模型。

Prometheus 抓取 API 的 `/metrics`。`PROMETHEUS_PORT` 非零时监控组件注册 Agent/工具指标；`/monitor` 提供工作台所用的运行摘要。场景数据校验默认不调用模型，真实回放使用独立 Runtime；基线仅在显式提升后生成。

## 场景执行与观测

API 与评测复用 `services/chat_service.py` 的完整链路。异步 Runtime 工厂负责资源所有权与失败回滚，评测空间使用唯一 `eval_<uuid>`；跨会话历史显式归档后才验证召回。规则和有证据 Judge 分层评分，报告只追加保存，比较时声明变化因素并固定控制条件。

`observability/` 提供可选 Langfuse 适配与真实模型用量计数；默认不启用外部导出。ContextRef、记忆差异和检索策略支持后续上下文实验。操作说明和验证边界见 [场景评测指南](evaluation.md)。

## 前端

Vue 工作台包含对话、知识库、评测页面。开发时通过 Vite `/api/python` 代理访问 FastAPI；容器版 Nginx 则将同一路径转发至宿主机 `8000` 端口。连接配置保存在浏览器本地，不存储 API Key。
