# Task 1 工具轨迹与实体一致性执行记录

日期：2026-10-26。范围：Task 1；未提交、未接入遥测，计划状态由主执行统一维护。

## 修改

- `backend/agents/agent_orchestrator.py`：删除实例 `_last_tools_used` / `_last_tool_traces`。`handle` 为每次调用创建独立 `AgentResponse` 执行容器，内部 `_call_llm(req, execution)` 返回该次调用结果；异常时保留此前完成的工具证据。
- 正常工具往返、工具拒绝/异常、模型后续异常及三轮上限路径均保留 trace；非法非对象输入不再导致构造 trace 时再次报错。白名单、参数验证与三轮上限继续生效。
- `tools_used` 记录通过白名单/参数验证且实际进入 handler 的调用，包括 handler 抛错；拒绝调用只进入 trace。trace 的 `success` 与 `result_success` 分别表达执行状态及工具返回的业务结果。
- Orchestrator 自行识别意图时补入识别实体，并保留调用方已有实体；降级到 GeneralAgent 时合并主 Agent 与降级 Agent 的工具记录。
- 保留 `AgentResponse`、`OrchestratorResult` 及 API 字段；仅内部 `_call_llm` 方法签名变化。
- `backend/tests/test_agent_orchestrator.py` 增加正常往返的 trace 断言；新增 `backend/tests/test_agent_trace.py` 覆盖失败、上限、交错并发、实体流转及降级证据。

## RED / GREEN

均在 `backend` 目录运行：

```powershell
../.venv/Scripts/python.exe -m pytest tests/test_agent_orchestrator.py tests/test_agent_trace.py -q
```

- RED：`14 failed, 8 passed in 19.66s`，退出码 1。失败分别暴露：正常返回缺 trace、拒绝/失败工具缺 trace、非对象参数导致二次异常、模型后续异常与最大轮次丢 tools_used、并发证据污染、识别实体未传递、降级覆盖原工具记录。
- GREEN：`22 passed in 24.93s`，退出码 0。
- 默认沙箱的 Windows asyncio 首次运行停在前两个测试，已中断；同命令使用 `require_escalated` 本机事件循环完成上述 RED/GREEN。未安装额外包，未调用真实模型或存储。
- `git diff --check` 退出码 0。
- 当前全套 `pytest tests -q` 曾启动；主执行通知 Task 4 仍在 RED 后主动中断，输出已进行 42 个通过标记，没有完整结果。集成后的全套验证由主执行完成。

## 边界与风险

- 新增测试使用确定性离线客户端；真实 Anthropic/业务工具联调未执行。
- `tools_used` 现在包含实际执行但抛错的工具。其状态需结合 trace 的 `success` 解读，不能视为业务已完成。
- 现有三轮上限保持原行为：第三轮仍请求工具时执行该轮工具后失败，不额外请求第四轮模型。
- 原始工具参数仍属于本地 trace 字段；未来遥测导出由独立脱敏层处理，本任务未导出数据。
- 仅修改指定实现/测试文件及本报告，没有回退其他任务的共享修改。
