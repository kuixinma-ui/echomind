# 开发与验证

## 环境

- 后端以 Python 3.12 为基准；Dockerfile 使用 `python:3.12-slim`。
- 前端需要 Node.js 20.19+ 或 22.12+。
- Redis、ChromaDB 和有效模型配置用于完整运行；单元测试使用假模型客户端。

## 在本机运行 Python

在 `backend/` 复制 `.env.example` 为 `.env`，填写模型配置，启动依赖：

```bash
docker compose up -d redis chromadb
python -m venv .venv
```

激活虚拟环境后：

```bash
python -m pip install -r requirements.txt
```

本机进程不使用容器内主机名，将 `.env` 中对应值调整为：

```dotenv
REDIS_URL=redis://:echomind-local-demo@localhost:6379/0
CHROMA_HOST=localhost
CHROMA_PORT=8001
CHROMA_PERSIST_DIRECTORY=./data/chroma
EVAL_BASELINE_PATH=./data/eval/baseline.json
ECHOMIND_SKILLS_DIR=./skills
```

如果修改过 Redis 密码，`REDIS_URL` 里的密码也需要一致。然后执行：

```bash
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

不要同时启动占用 `8000` 端口的后端容器和本机 Python 服务。

## 前端开发

在 `frontend/` 执行 `npm ci` 和 `npm run dev`。Vite 默认代理至本机 `8000` 端口；如需覆盖目标，可在前端 `.env.local` 中配置：

```dotenv
VITE_PYTHON_API_URL=http://localhost:8000
```

前端环境变量会进入浏览器代码，只能填写可公开的地址，不能填写模型密钥。

## 现有单元测试

在 `backend/` 的虚拟环境中：

```bash
python -m pip install -r requirements-test.txt
python -m pytest tests -q
```

离线测试覆盖 Agent、工具轨迹、完整对话服务、隔离回放、评分与报告、上下文版本和记忆差异。它们不验证真实模型质量、Redis/ChromaDB 连接或完整部署；数据校验、live 回放和观测配置见[场景评测指南](evaluation.md)。

前端构建验证：

```bash
cd frontend
npm ci
npm test
npm run build
```

仓库 CI 运行上述 Agent 单元测试和前端生产构建；完整对话仍需配置模型和数据服务后手动验证。

## 容器与常用命令

后端在 `backend/`：

```bash
docker compose ps
docker compose logs -f echomind
docker compose down
```

前端 Dockerfile 使用已生成的 `dist/`，所以在 `frontend/` 先执行 `npm ci`、`npm run build`，再执行 `docker compose up -d --build`，访问 <http://localhost:5174>。

后端默认端口为 API `8000`、Chroma `8001`、Redis `6379`、Prometheus `9090`、Nginx `80`。Compose 用于本地演示，发布到服务器前需要配置鉴权与网络访问范围。
