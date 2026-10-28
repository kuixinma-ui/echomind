# mas-agent Python Backend

FastAPI 入口为 `api/main.py`，对话链路包含意图识别、ChromaDB 知识检索、多 Agent 路由和 Redis/ChromaDB 记忆。

从本目录启动：

```bash
cp .env.example .env
# 编辑 .env：填写模型 API Key 并确认模型名称。
docker compose up -d --build
```

PowerShell 复制命令为 `Copy-Item .env.example .env`。

- API：<http://localhost:8000>
- Swagger：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- Prometheus：<http://localhost:9090>

前端在 `../frontend`，单独执行 `npm ci` 和 `npm run dev`。

参考[项目首页](../README.md)、[架构说明](../docs/architecture.md)和[本地开发指南](../docs/development.md)。
