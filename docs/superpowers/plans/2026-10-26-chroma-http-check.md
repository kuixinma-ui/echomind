# Chroma 真实 HTTP 显式向量联调记录

**时间：** 2026-10-26 15:44（Asia/Shanghai）

**结论：** Chroma 0.5.23 的真实 HTTP 向量读写回环通过，集合删除成功；Redis 仍不可用，完整联调未完成。额外清理读回脚本使用了错误的 `404` 预期，导致该次归档总状态为 `failed`；原始报告保留，不覆盖或重新标为通过。

## 环境与范围

| 项目 | 实际值 |
| --- | --- |
| Python | 项目 `.venv-integration`，3.11.16 |
| Chroma 服务/SDK | 0.5.23 / 0.5.23 |
| 其他包 | httpx 0.28.1、FastAPI 0.115.5、Pydantic 2.10.3、NumPy 2.4.6、PostHog 7.75.11 |
| 部署 | 临时原生 Chroma HTTP 服务，非 Docker |
| 地址 | `127.0.0.1:18001`，启动前确认无监听 |
| 配置 | `ANONYMIZED_TELEMETRY=false`、`IS_PERSISTENT=true`、独立持久化目录 |
| 目录 | `backend/data/integration/chroma-http-f1ab706f0e98482fa72362b1b0e302a7/server-data` |
| 模型/密钥 | 不读取 `.env`，不调用 LLM，不下载 embedding 模型；报告 `model_requests=0` |
| 存储测试 | 仅本次 UUID 集合、合成用户/会话/文档、显式三维测试向量 |

通过项目 Python 启动 `python -m chromadb.cli.cli run --host 127.0.0.1 --port 18001 --path <本次独立目录> --log-path <本次日志>`，然后直接向 `run_checks` 传入配置字典并指定 `stage='storage'`。没有调用配置文件加载函数。

## 实际证据

归档 run_id：`integration_58af36359da94b899d000a7b0e91f761`。

- [原始脱敏 JSON](../../../backend/data/integration/chroma-http-f1ab706f0e98482fa72362b1b0e302a7/integration_58af36359da94b899d000a7b0e91f761.json)
- [原生服务日志](../../../backend/data/integration/chroma-http-f1ab706f0e98482fa72362b1b0e302a7/server.log)
- [Uvicorn 日志](../../../backend/data/integration/chroma-http-f1ab706f0e98482fa72362b1b0e302a7/server-uvicorn.log)

以上证据保存在 Git 忽略的本地运行目录中，不随代码提交。

| 检查 | 结果 | 证据与解释 |
| --- | --- | --- |
| 服务启动 | passed | 本次创建的进程在回环地址提供真实 HTTP 服务 |
| `GET /api/v1/heartbeat` | passed | HTTP 200 |
| `GET /api/v1/version` | passed | HTTP 200，返回版本 `0.5.23` |
| 创建唯一集合 | passed | HTTP 200，集合 `eval_d05c3e530988444890e3d835bfe6c733_integration` |
| 显式向量写入 | passed | `POST .../add` HTTP 201 |
| 带用户/会话 `$and` 条件检索 | passed | HTTP 200，返回预期 ID `probe-1` |
| 集合删除 | passed | `DELETE .../collections/<name>` HTTP 200；`probe_chroma` 返回 `cleanup='passed'` |
| 额外 HTTP 清理读回 | 断言不匹配 | 缺失集合 GET 实际为 HTTP 400；临时脚本错误要求 404，因而把总报告标记为 failed |
| 停服后独立持久化库只读检查 | passed | 以 SQLite `mode=ro` 执行 `select name from collections`，结果 `[]` |
| Redis | blocked | 启动前 `16379` 无监听；实际连接抛出 `ConnectionError`，未写入 Redis |
| 本次服务终止 | passed | 本次 launcher PID `23200` 与服务 PID `35316` 均已退出，`18001` 无监听 |

`probe_chroma` 本身记录的 `status='passed'`、`cleanup='passed'` 与额外脚本的总状态 `failed` 是两层结果。不能用额外断言失败推断业务集合残留，也不能隐藏该次归档的失败状态。已安装 Chroma 源码中，`InvalidCollectionException` 继承的 `ChromaError.code()` 返回 `400`；本次额外请求只记录了 HTTP 状态，后续 SQLite 空集合读回直接确认了清理结果。

本次只运行了一次成功的 HTTP probe。没有为修饰报告重新启动服务或重复回环，也没有修改评测生产代码。

## 发现的依赖兼容问题

服务日志出现 `capture() takes 1 positional argument but 3 were given`。已安装的 PostHog 是 `7.75.11`，而 Chroma 0.5.23 的适配层仍以三个位置参数调用 `posthog.capture(user_id, event_name, properties)`。即使 `anonymized_telemetry=false` 令 `posthog.disabled=True`，Python 仍先执行函数参数校验，因而出现该日志。

此错误没有阻断本次 HTTP 存储回环。没有据此声称发生遥测外发，也没有在本任务中更改依赖或生产代码；已将兼容问题交给主任务处理。

## 尚未覆盖

- 默认 ONNX embedding 的下载、缓存与真实文本向量质量（报告 `default_embedding_verified=false`）。
- Redis 的真实服务、TTL 与隔离语义。
- Langfuse 平台的真实 trace/score 写入及读回。
- 服务重启后的持久化恢复、Docker 镜像健康检查、负载与并发性能。
- 模型、工具循环、Judge 及业务专家标注审核。

因此，这项证据只支持“原生 Chroma 0.5.23 的显式向量 HTTP 回环和本次集合清理通过”；`full_integration_verified` 继续为 `false`，不提升场景 baseline。
