# mas-agent Frontend

面向 mas-agent Python API 的 Vue 3 工作台，提供对话、Agent 路由与工具轨迹查看、知识库检索与导入、Skills 管理和评测结果展示。

## 本地开发

需要 Node.js 20.19+（20.x）或 22.12+，以及 npm。先按[项目首页](../README.md)启动 Python 后端，确认 `http://localhost:8000/health` 返回 `ok`。

在 `frontend` 目录执行：

```bash
npm ci
npm run dev
```

打开 `http://localhost:5173`。前端默认连接 `/api/python`，Vite 将请求代理到 `http://localhost:8000`。用户 ID 和会话 ID 可在连接配置中调整，连接设置保存在浏览器本地。

若 Python API 使用其他地址，在 `frontend/.env.local` 中配置后重新启动开发服务：

```env
VITE_PYTHON_API_URL=http://localhost:8000
```

## 构建

```bash
npm ci
npm run build
```

静态文件生成到 `dist/`。`npm run preview` 仅用于检查静态构建；Vite 开发代理不适用于 preview，完整联调可使用下面的 Nginx 部署，或构建前将 `VITE_PYTHON_API_URL` 设为可访问的 API 地址。

## Docker 部署

先启动宿主机上的 Python 后端，再在 `frontend` 目录执行：

```bash
npm ci
npm run build
docker compose up -d --build
```

必须先生成 `dist/`：Dockerfile 只负责将构建结果复制到 Nginx 镜像。打开 `http://localhost:5174`，Nginx 将 `/api/python/` 代理到宿主机 `8000` 端口；修改后端端口时需同步调整 `docker/nginx.conf`。

```bash
docker compose down
```

前端只保存连接与会话设置，模型 API Key 配置在后端环境变量中。
