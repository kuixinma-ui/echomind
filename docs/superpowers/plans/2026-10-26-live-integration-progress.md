# 真实联调执行记录（2026-10-26）

当前结论：**partial，尚未完成全链路验收**。可用的模型和本地SDK边界已经实测；真实Redis、默认embedding、Langfuse平台落库/读回及完整场景回放仍待环境就绪。没有提升或覆盖baseline。

## 已执行与证据

| 项目 | 结果 | 范围与证据 |
| --- | --- | --- |
| 默认预检 | 通过安全边界测试；实时状态blocked | 只读包版本与配置存在性，0模型请求、0存储请求；`integration_aeed6a89c47a4f8fa5fa0d945c383f6d.json` |
| DeepSeek协议 | passed | 一次归档合成请求，输出上限64；输入88、输出27 Token，4156ms，thinking+text，end_turn；`integration_034cac1897c24fd4bdb6b10f3a93154a.json`。不代表场景质量 |
| 本机存储预检 | blocked | 当时Redis16379和Chroma18001未监听；`integration_8112a39b4da046dca7858777a8e007b1.json`，没有fallback为本地通过 |
| Chroma本地持久化 | passed | 真实0.5.23，两个独立子进程写入/重开，显式向量、过滤与跨用户隔离、只删自建集合；非默认embedding、非HTTP部署验收 |
| 原生Chroma HTTP补测 | 数据回环通过；原始总报告failed保留 | heartbeat/version/create/add/query/delete通过，DELETE200；额外脚本误期望GET404，实际0.5.23缺失集合返回400。停服后SQLite确认collections=[]；详见chroma-http-check.md。Redis仍blocked，不能算完整存储通过 |
| Langfuse SDK/协议 | passed | 真实5.0.4、OTel内存导出及score的MockTransport请求；父子关系、usage、datetime、正文默认关闭/opt-in脱敏。没有连接Langfuse平台 |
| 完整后端回归 | passed | 最终Python3.11完整依赖环境 **212 passed in 26.11s**，无skip；JUnit：`backend/data/integration/pytest-python311.xml` |
| 场景文件校验 | validated | 120条、135轮、702断言、8组、19意图；状态仍为synthetic_pending_review |
| Compose配置 | 静态通过，容器未运行 | PyYAML解析9服务、4个localhost端口、6个Langfuse组件；公开manifest/digest核对；未执行docker compose config/build/up |

上表JSON和依赖清单位于被Git忽略的 `backend/data/integration/`；不包含凭据或模型回复正文。原Python3.12环境阶段回归为208 passed/1 skipped（未装Chroma时跳过本地测试）；之后新增的Redis异常回归在完整Python3.11环境一并验证。

最终默认预检已在受限Windows环境直接执行成功（报告状态仍按定义为blocked，表示未执行实时验收），不创建事件循环；新归档为 `integration_35d78758a60e4dbdb46291cc9d8a18ef.json`。PostHog固定5.4.0后，完整212项测试及pip check通过，Chroma跨进程测试不再出现capture签名错误。原HTTP补测的failed归档保持原样。

## 实现修复与独立复核

- 存储显式支持auto/http/local。历史在线默认保留auto；隔离评测默认严格http，错误不会被PersistentClient降级掩盖。可明确选择local，但不能当远端验收。更正了默认embedding在客户端执行的注释。
- 真实SDK契约先RED（7失败/2通过）再修复日期与Anthropic内容块；任意对象、extras、默认关闭正文仍保持拒绝导出。
- 场景确定缺陷先RED（9失败/13通过）再修复：赔偿500元误路由、升级话术虚假跟进承诺、历史文字沟通线索丢失。
- 独立复核发现Redis验证使用assert及关闭异常丢证据，已改显式检查，关闭失败保留namespace/cleanup，其他组件继续探测；关闭异常回归先RED再GREEN。
- 新环境暴露测试constraints包含uvicorn extras导致pip拒绝，已将测试清单改为匹配生产的明确裸包版本；新环境真实安装及pip check通过。
- Redis INFO/version只证明兼容协议，报告明确server_identity未验证；后续结合实际Compose镜像/process来源验收，不能把fakeredis视为真实Redis。
- 真实HTTP日志暴露Chroma0.5.23仍使用旧版PostHog capture签名，与新解析的7.75.11不兼容；新增真实Chroma子进程stderr回归并固定PostHog5.4.0。默认预检改为同步执行，避免无网络检查也创建Windows asyncio socketpair；两项均先复现失败再修正。

详见 [SDK与联调审计](2026-10-26-integration-review.md)、[120条场景技术预审](2026-10-26-dataset-review.md)、[原生Chroma HTTP记录](2026-10-26-chroma-http-check.md)。

## 环境与可重复执行

原Python3.12安装完整Chroma时因缺MSVC失败，没有安装系统编译器。现有portable Python3.11有匹配的chroma-hnswlib 0.7.6轮子，因此在项目内创建 `.venv-integration`，固定Chroma0.5.23、Anthropic0.40.0、Redis客户端5.2.1、Langfuse5.0.4；`pip check`通过，完整freeze保存为 `backend/data/integration/requirements-freeze-python311.txt`。生产仍使用原Python3.12配置，本机联调环境没有替代它。

```powershell
# 从发布项目根目录执行，无付费请求。
$env:PYTHONUTF8='1'
$env:PYTHONPATH='backend'
.\.venv-integration\Scripts\python.exe -m pytest backend/tests -q
.\.venv-integration\Scripts\python.exe -m evaluation.scenario_runner --mode validate
```

完整依赖的间接版本尚未全部锁定；freeze是本次环境证据，不是跨平台锁文件。Docker镜像构建和Linux运行兼容性仍需实际验证。

## 尚待完成的验收

1. 本机安装并启动Docker Desktop/WSL环境，按 [联调指南](../../integration.md) 生成本地忽略配置、启动Redis和Chroma。需要系统启用/安装/协议接受/重启时由用户决定，项目脚本不自动执行。
2. `--stage storage`真实验证Redis PING、列表、TTL、隔离与清理；固定服务来源。原生Chroma HTTP基础回环已补测；Docker部署与默认ONNX缓存/中文检索仍需分别验收，显式测试向量不能代替默认embedding。
3. 在本地Langfuse创建测试项目并配置keys，实际读回同一run/case的trace和score。SDK测试、flush及health不足以替代落库证据。
4. 环境齐备后最多3条合成场景回放，再判断是否扩大范围；Judge固定、记录实际调用数与用量，超时/无效/清理失败保留。
5. 业务确认account/account_security两例、knowledge_boundaries_11固定升级节点却强制检索的矛盾等。未经确认不修改标签或宣称120条业务通过。

本阶段前端未改，沿用上一阶段3项测试和构建证据。变更仍留在本地工作分支，未提交、推送或部署；没有复制原项目密钥，也没有安装/启用WSL/Docker。

## 用户继续执行后的补充

进入[Windows运行环境准备](2026-10-26-windows-runtime-setup.md)：官方安装包已核验SHA256与签名，但Docker per-user安装因VirtualMachinePlatform未启用退出1，WSL正常UAC授权未完成（Windows返回取消）。系统仍需管理员操作和重启，Redis/Langfuse链路未运行。

项目侧已生成忽略的本地服务配置及3条smoke子数据集。准备回放时发现evaluation知识被public默认范围过滤，已修复runtime持有的scope传递并防止调用方扩大范围；先RED 5失败/1通过，完整回归更新为 **218 passed in16.43s**，JUnit无errors/failures/skipped。没有为环境阻塞反复调用收费模型，也没有修改原数据标签或提升baseline。

最新进展：用户已成功安装WSL3.0.3并启用VirtualMachinePlatform，Windows提示重启后生效；本机 `wsl --version` 已核验退出0。当前等待用户重启，之后继续Docker安装；安装包、服务配置、smoke子集与回归记录均已保留。详见Windows运行环境记录的重启前交接。

## 重启后最终状态

用户已重启，Docker安装/Compose校验/8服务healthy均完成。真实Redis、Chroma默认embedding及Langfuse trace/score已验收。首轮3条为1通过/2 invalid；定位DeepSeek thinking耗尽结构化输出预算后，显式禁用结构化thinking并共用安全JSON解析，同3条回归 **3/3通过、0 invalid/timeout/error/hard_fail，清理完成**。首轮记录保留，未提升baseline。

最终全套后端 **257 passed in53.22s**。完整run_id、用量、观测读回、健康检查修复和剩余范围以[重启后结果](2026-10-26-live-integration-results.md)为准，前文各环境阻塞属于历史记录。
