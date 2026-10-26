# Billing角色账号SOP补齐（2026-10-26）

本任务属于候选演示规则的实现补齐，**不是业务专家审核**。没有修改account/account_security到billing的既有路由，没有修改场景数据或真实执行账号操作。

## 问题与最小改动

原SkillManager先按agent筛选，再按当前消息关键词匹配。现有billing_support只覆盖资金词；纯账号、隐私和保存请求无法注入账号SOP。BillingAgent的角色输入包还对每种意图统一列出订单号/支付金额为missing_fields。

新增 `backend/skills/account_support/SKILL.md`，按现有格式配置agents=billing，覆盖账号资料、异常登录、密码重置、冻结、他人隐私、记忆保存/删除等关键词。内容明确：

- 一般资料变更说明官方入口与身份核验，异常登录等明确安全事件提供官方安全处置建议；已有上下文明确的信息不重复索要。
- 拒收密码、短信验证码、完整银行卡号和他人信息；不复述或扩散已经发送的敏感内容。
- 未接账号后台执行工具，不宣称已经变更手机号、冻结或删除持久化数据。
- 记忆保存/删除请求只说明当前能力；不承诺已关闭保存、改变保留期限或执行删除。
- 纯账号问题不机械索要订单或金额；如另有资金争议，继续现有账单核验流程。

BillingAgent仅做对应的小幅调整：

- 角色契约和system prompt明确资金/账号分支及各自最少核验信息。
- 当已识别意图为ACCOUNT或ACCOUNT_SECURITY，确定性输入包采用account范围与官方身份核验说明，不列订单/金额字段。
- 其他意图保留原verification_fields及missing_fields计算。资金工具白名单、路由、模型参数和Token上限均未改。

## RED → GREEN

先新增 `backend/tests/test_account_skill.py`，修复前结果：**11 failed / 1 passed**。失败集中在7类账号/隐私/保存请求无SOP、两个账号意图仍要求资金字段、缺少后台/保存边界及混合场景匹配。原资金流程测试在RED阶段已通过。

最小实现后运行：

```text
PYTHONPATH=backend
.venv-integration/Scripts/python.exe -m pytest backend/tests/test_account_skill.py backend/tests/test_agent_orchestrator.py backend/tests/test_agent_trace.py -q
38 passed in 2.82s
```

测试使用真实SkillManager及BillingAgent的prompt/role packet构造；仅Provider为离线FakeClient，没有模型或服务调用。Windows本地socketpair权限由require_escalated提供，未改变测试语义。

额外静态检查：`git diff --check`通过。实测账号Skill prompt817字符，账号与资金同时命中3069字符，均在配置的5000字符预算内；其他角色不会注入本billing专用SOP。

## 范围与限制

- 规则的可发现性、注入、分支信息边界及原资金流程已离线验证；这些测试不证明真实LLM一定遵守全部SOP。
- 未新增账号变更、冻结、持久化删除或保存策略工具；这类能力仍须真实业务系统与授权链路，不能由prompt冒充。
- Skills现在有4个内置目录；新规则会自然进入后续skills指纹。对候选数据的真实回放和完整回归由主执行者统一执行。
- 未读取 `.env`、调用付费模型或服务、提交/推送代码。
