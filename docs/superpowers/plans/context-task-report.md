# Task 7: ContextRef、画像差异审计与可控检索

## 完成范围

- 新增 `backend/context/`：不可变 `ContextRef`、ISO 时间比较、确定性版本排序、JSON 画像差异。
- 修改 `KnowledgeBase`、`MemoryManager`、`MCPToolManager`，新增 `test_context_storage.py` / `test_context_retrieval.py`。
- 保留 Task 4 的客户端注入、默认 Redis key / Chroma collection、唯一实验 namespace 和精确 cleanup。
- 没有安装依赖、提交、联网模型调用或改动 runtime / API / agent / evaluation / frontend。本报告为明确分工允许的文档交付。

## 接口与行为

### ContextRef 与知识

`from context import ContextRef`；字段为 `id/type/version/source/scope/effective_at`，`to_dict()` 保留这些字段。Chroma metadata 使用 `context_id/context_type/version/source/scope/effective_at` 扁平字段，兼容 Chroma 标量值限制。

`KnowledgeBase.add_documents()` 接受原有 `title/content`，也接受顶层 ContextRef 字段或 `context_ref` 对象/字典。旧输入生成确定性文档 ID，默认 version `1`、scope `public`、effective_at `1970-01-01T00:00:00Z`。切片 ID 即 `ctx_` 加 `[doc_id,type,version,scope,chunk]` 的 SHA-256，同文档不同版本可以并存、准确定位。

`search(query, top_k=5, *, scope='public', as_of=None, versions=None)`；异步版本同样接受过滤参数，tool handler 转发显式 `scope/as_of/versions`。

- 默认仅查询 public；指定 scope 时允许 public 加该 scope。scope 是调用方选择的适用范围，**不是生产认证或 ACL**；生产权限需要可信 principal 决定可读范围。
- `as_of` 接受 ISO 时间，统一转为 UTC；缺省为当前 UTC。未来版本不参与召回。
- 每个 `(doc_id, scope)` 按 `(effective_at, 自然版本顺序)` 确定有效版本，再保留向量排序并取 top-k。`versions={'refund':'1'}` 可显式定位仍已生效的旧版本。
- 返回原有 `title/content/score/chunk`，新增 `context_ref/source/evidence_id`。`context_ref.id` 是文档级 ID；`evidence_id` 是版本/切片级 ID，两者不能互相替代。
- 当前用全 collection 候选过滤保证旧版高向量分不能挤掉新版；大知识库需要后续引入版本 manifest 和服务端过滤以减少开销。

### 用户画像与审计

`MemoryManager(..., client=None)` 允许注入模型客户端，保留 Task 4 Redis/Chroma 注入接口。`update_profile(user_id, conv_id) -> bool`：成功 True，模型/存储失败 False，无消息时 True（无操作）。旧调用方忽略返回值仍可使用。

画像通过一次 Chroma `upsert` 替换，取消 delete/add 空窗。模型提取提示处理更正/删除，持久 metadata 与审计明确 `inferred=True`；prompt 内也声明画像可能包含模型推断，并非已核实业务事实。每个用户在同一 MemoryManager 内串行更新，以保持差异基线与版本顺序。

`get_profile_audit(user_id)` 返回最近 10,000 条变更，按时间从旧到新排序。条目包含：

- `added`：新增路径到新值；`updated`：路径到 `{before, after}`；`deleted`：删除路径到旧值。嵌套 dict 使用点路径，列表整体替换。
- `source_conv/version/inferred/experiment_seed/context_ref/updated_at`。版本从 `1` 递增。
- Redis key 使用原 Task 4 `memory_key(namespace, 'profile_audit', user_id, '')`，按用户与运行隔离；Task 4 cleanup 的精确 namespace 扫描一并删除审计，不触及另一次运行与默认线上数据。

`seed_profile(user_id, profile, *, source_conv='scenario_initial_state') -> audit` **仅用于实验初始状态**，强制 `eval_<32hex>` 唯一 namespace；默认生产或普通 namespace 会拒绝。审计标记 `inferred=False, experiment_seed=True`。已与 root / scenario agent 约定；runner 当前仅使用初始 messages，不必依赖此接口。

画像与 Redis 审计跨两个存储，不宣称分布式事务；审计写入失败会返回 False，但 Chroma 的新画像可能已写入。多进程同时写同一生产画像仍需存储端并发控制，这不属于当前实验隔离保证。

### 检索策略、计数、遥测

`MCPToolManager(api_key, base_url=None, model=..., *, rewrite_policy='always', rerank_policy='always', client=None, retrieval_timeout_s=30.0)`。原有三个位置参数保持兼容。策略只接受 `always/never/adaptive`。

- `always` 为旧默认：改写查询；只有候选数超过 top-k 才实际重排。
- `never` 跳过对应模型调用；重排关闭仍截取 top-k。
- `adaptive` 规则版本 `1`：短问候/感谢/告别或含明确订单号的结构化查询不改写；其他查询改写。只有候选数 `> max(5, top_k * 2)` 才重排。
- 模型改写、重排均受 `retrieval_timeout_s` 限制，超时取消对应协程并回退。改写输出必须是字符串数组；重排输出必须是完整、不重复、合法整数索引排列，否则回退原排序。
- 去重优先使用 `evidence_id`，旧数据忽略 `score/distance/rerank_score` 后比较，避免同证据不同分数重复。保留首次召回的证据项。
- `ToolResult.reranked` 只有模型实际成功返回有效排序才 True。无需重排、策略跳过、超时、异常、无效输出均 False；缓存保留实际布尔状态。
- `retrieval_snapshot()` 只返回配置：`rewrite_policy/rerank_policy/adaptive_rules_version/retrieval_timeout_s`。动态计数不进入 fingerprint。
- `retrieval_stats()` 单独返回 `searches/rewrite_calls/rewrite_fallbacks/rerank_calls/rerank_fallbacks/candidate_count` 的累积实际计数；日志记录子查询数、唯一候选数，不新增查询正文日志。
- 使用 lazy `observability.telemetry.get_telemetry().span(...)`，遵守当前请求绑定；span 名为 `retrieval`（retriever）、`retrieval.rewrite` / `retrieval.rerank`（generation）。

实验相同快照配对比较、配置不匹配拒绝、trace 文档级 ID 由 scenario / integration 分工处理。本任务已通知其同时使用 `context_ref.id`（document_ids）和切片级 `evidence_id`，以及把 `adaptive_rules_version='1'` 纳入 fingerprint。

## TDD 与验证证据

所有命令均在 `backend` 目录，用现有 `../.venv/Scripts/python.exe`、离线 fake Redis/Chroma/模型运行；Windows asyncio 通过 require_escalated 执行，无网络。

1. 首轮 RED：`python -m pytest tests/test_context_storage.py tests/test_context_retrieval.py tests/test_runtime.py -q` → **17 failed, 3 passed**。失败为 ContextRef 模块缺失、版本过滤参数缺失、scope 数据泄漏、审计/seed 缺失、策略/模型注入 constructor 缺失。原 Task 4 runtime 三项通过。
2. `test_profile_update_returns_explicit_failure_instead_of_silent_success` 单独 RED → **1 failed**，`None is False`，证明内部异常吞掉后调用方无法判断失败。
3. 首轮 GREEN → **21 passed in 32.49s**。
4. 模型客户端注入/推断 prompt 用例先 RED → constructor 不接受 `client`；仅加注入后第二次 RED → prompt 缺“模型推断”说明。随后才补 prompt 声明。
5. 两个相关原有 memory 问题各自独立 RED → **2 failed**：真实 Chroma 式严格 fake 拒绝双键 where 后 history 为 `[]`；压缩保留消息从 `14→10` 反序。对应只做 `$and` conjunction 和 `for m in keep` 两处窄修复。
6. 补充验证 adaptive 正向分支、真实 wait_for 超时取消、缓存保留实际 rerank 状态；专项命令同 1 → **28 passed in 23.24s**。
7. 最后清理后重复专项命令 → **28 passed in 44.62s**，`git diff --check` 无错误（仅其他共享工作文件的换行提示）。这些执行耗时不是检索性能结论。

测试检查了真实 KnowledgeBase/MemoryManager/MCPToolManager 的输出与持久化结果；仅外部客户端用 fake，严格 fake 同时模拟 Chroma where 约束与用户过滤。未连接真实 Redis/Chroma、未测 live 模型质量或给出性能改善数字。root 负责最终全套离线测试和整体集成。
