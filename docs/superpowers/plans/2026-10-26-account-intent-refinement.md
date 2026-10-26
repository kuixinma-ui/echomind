# 账号资料变更与安全意图细化回归

日期：2026-10-26。此修改采用本阶段的**演示口径**，不是业务专家批准，不改变数据集的待业务审核状态。

## 定义与根因

现有taxonomy把 `account` 定义为账户管理；模板包括修改邮箱、注销账户、更新个人信息。`account_security` 模板则是账户被盗、异常登录、重置密码。本阶段采用：一般资料变更保持account；明确的安全事件或安全功能任务可细化为account_security。

原关键词列表包括裸词“安全”。弱关键词模式可把泛类识别细化成专类：当融合最佳是account、低于0.8且安全模式置信度至少0.5时，会直接改为account_security。因此：

| 原样例 | 正确LLM边界输入 | 修复前实际 |
| --- | --- | --- |
| technical_security_12：“旧手机号停用了，怎样安全更换账户绑定手机？” | account，confidence=1.0 | account_security；融合分约0.734，refined_by_pattern=0.5 |
| cross_session_memory_07：“我过去说的手机号已经作废，怎样通过官方入口安全更换？” | account，confidence=1.0 | account_security；融合分0.7，refined_by_pattern=0.5 |

这里的“安全”描述执行方式，没有明确的异常登录、盗号或安全设置任务。问题在于弱副词被当作专类证据；不需要为匹配该规则修改原文本或原标签。

## 最小修复

`backend/core/intent_recognizer.py` 的ACCOUNT_SECURITY关键词改为：被盗、异常登录、重置密码、两步验证、账户安全、账号安全、安全设置。只移除裸“安全”的强制细化资格；原投票、权重、阈值、缓存和路由保持原逻辑。

该规则不阻止LLM根据实际语义返回account_security，也不强行把所有带“安全”的消息归account。它只约束弱关键词细化的证据。现有正常账号角色路由仍是billing，由父任务处理其SOP覆盖。

没有修改 `customer_service_v1.json`、示例标签、Skills、LLM兼容逻辑、runtime或MCP。

## RED → GREEN

新测试文件：`backend/tests/test_account_intent_refinement.py`，11例：

- 3例一般资料变更（含原两个完整文本），检查保持account且没有pattern refinement。
- 8例明确安全事件/功能（账户安全、账号安全、安全设置、异常登录、被盗、重置密码、两步验证，以及资料变更与异常登录同句），检查仍可从泛类account细化到account_security。

只在外部LLM响应边界返回预设account/confidence1.0；实际recognize、模式、三路投票与本地字符embedding全部执行。通过 `__new__` 避免构造真实API客户端，不读取.env，不调用模型/服务。

修复前运行新文件：**2 failed、9 passed in1.40s**。两例失败正是原始边界样例，而明确安全事件的保护样例已通过。

修复后运行：

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONPATH='backend'
.\.venv-integration\Scripts\python.exe -m pytest backend/tests/test_account_intent_refinement.py backend/tests/test_intent_recognizer.py backend/tests/test_agent_orchestrator.py -q
```

结果：**33 passed in1.51s**。运行使用正常本机权限允许Windows asyncio socketpair，未访问真实模型或业务存储。完整后端验证由主执行者统一完成。

## 实际限制

这是固定边界的离线回归，不证明真实LLM一定返回account，也不能替代账户恢复、安全风险、身份核验等业务政策审核。自然语言歧义和完整账号SOP由候选数据审核及后续真实分批回放继续验证。
