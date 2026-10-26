# Task 6 独立遥测模块执行记录

## 交付范围

- 新增 `backend/observability/telemetry.py`、`backend/requirements-observability.txt`、`backend/tests/test_telemetry.py`。
- ChatService、Agent、API、检索及 runner 接入由主任务负责，本子任务没有编辑这些文件。
- 公开接口：`Telemetry` / `Observation` 协议、`NoopTelemetry`、`LangfuseTelemetry`、`get_telemetry()`、`bind_telemetry(adapter)`。
- `span(name, *, kind='span', input=None, metadata=None)` 是同步上下文管理器，yield 对象支持 `update(**fields)` 和数值 `score(name, value)`；适配器提供 `flush()` 和 `shutdown()`。

## 默认行为与隐私

默认返回 Noop，既不读取 `LANGFUSE_*` 凭据，也不导入 SDK 或创建客户端。仅 `ECHOMIND_TELEMETRY_ENABLED=true` 开启导出；缺少配置、SDK 或构造器失败时退回 Noop。SDK 生命周期任一步骤失败仅记录固定操作名与异常类型，不记录消息、栈、正文或配置。

`ECHOMIND_TRACE_CONTENT=true` 独立开启正文。默认隐藏 input/output 及 metadata 的正文键和复合正文键；递归隐藏凭据、直接联系信息、邮箱/中国手机号/Bearer 等可识别值，异常对象仅保留类型。任意对象不会通过 `repr` 泄漏信息，递归深度受限。字段名脱敏和模式匹配无法替代人工审查：调用方仍应只传递结构摘要与受控键，尤其不要将敏感信息编码进 span 名称或 metadata 键名。

`ECHOMIND_TELEMETRY_HASH_SALT` 控制 user_id / conv_id / conversation_id / session_id 的 HMAC-SHA256。配置相同 salt 可跨进程关联；未配置则使用进程随机 salt，禁止以无盐 hash 导出身份。salt 和原始标识不会进入观测。身份目前作为 metadata 导出，不使用旧版 trace setter。

业务异常原样重新抛出。SDK 上下文退出接收 `(None, None, None)`，避免 SDK/OTel 自动记录业务异常消息与堆栈；显式导出 ERROR 与异常类型。SDK 的 suppression 返回值不会吞掉业务错误。请求绑定使用 ContextVar 并按 token 恢复；SDK 的 current observation 管理嵌套父子关系。

## SDK 核验（2026-10-26）

- [PyPI langfuse](https://pypi.org/project/langfuse/) 当前发布文件明确为 **5.0.4**，上传日期 2026-10-21；独立可选文件固定 `langfuse==5.0.4`。
- [官方 Python reference](https://python.reference.langfuse.com/langfuse) 同样报告 `__version__='5.0.4'`。使用 `Langfuse(public_key=..., secret_key=..., base_url=..., debug=False)`、`start_as_current_observation(name=..., as_type=..., input=..., metadata=...)`、observation `update/score`、客户端 `flush/shutdown`；没有使用旧 `trace()`、`start_span()` 或已删除 trace IO setter。
- [官方 v4 → v5 迁移说明](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v4-to-v5)：需要 Python **>=3.11**，Langfuse Cloud 或自建 **>=4.59.0**。v5 的 observation update/score 继续可用。自建 v3 或旧 v4 不能据此声称兼容。
- 可选依赖不加入 `backend/requirements.txt`，不修改现有 Anthropic 版本。安装示例：`python -m pip install -r backend/requirements-observability.txt`。

## RED / GREEN 验证

使用根目录 `.venv/Scripts/python.exe`，设置 `PYTHONPATH=backend`。

1. 实现前运行 `python -m pytest backend/tests/test_telemetry.py -q --tb=short`：**23 failed**，均因 `observability` 模块不存在。
2. 首次实现后 22 passed / 1 failed，失败原因是 update 失败用例只更新默认已隐藏的 output；修正测试为同时更新结构 metadata，确保真正触及 SDK update 导出边界。
3. 23 条专项 **23 passed in 1.88s**；当时全套 `python -m pytest -q --tb=short` 为 **86 passed in 85.09s**。
4. 补充复合正文键、SDK 构造失败及共享适配器并发根观测测试：**25 passed / 1 failed**，证实 `last_user_message` 等复合键漏正文；随后修复复合键脱敏。
5. 最终专项共 **26 条**：覆盖禁用不加载 SDK/不读凭据、缺配置/缺依赖/构造失败、递归隐私与 salt、start/enter/update/score/exit/flush/shutdown 全阶段故障、异常原样传播、嵌套父子与同适配器并发隔离、绑定恢复与生命周期。最终专项命令同第 1 步，最近一次 **26 passed in 0.56s**。

Windows 受限沙箱中 asyncio 用例挂起；相同命令使用 `require_escalated` 后正常结束。已检查并停止本子任务产生的两个旧挂起进程。最终全套由主任务统一复验。

## 尚未执行

未安装或启动真实 Langfuse 服务，未读取真实凭据，未运行外部服务/模型/付费调用；SDK 边界测试使用按 v5 接口构造的 fake client。因此 live 上报、后台 exporter 重试行为与真实服务端展示不属于已验证结论。
