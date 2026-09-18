# Ai Ink 本地部署与验证

当前交付目标是本地演示。登录使用已有用户名，无密码验证；`APP_ENV=prod` 会拒绝演示 token 签发。公网账户、模型密钥用户级 RLS 与登录防撞库见 [PROD-CREDENTIALS.md](PROD-CREDENTIALS.md)，上线时按该方案做，不要把本文演示配置原样暴露公网。

## 启动应用

安装并启动 Docker Desktop，在项目根目录执行：

```powershell
Copy-Item .env.example .env  # 仅首次执行，已有 .env 时保留原配置
# 生成章节前在前端「环境配置」填写模型 Key，不要写进 .env
docker compose up -d --build
docker compose ps
```

打开 http://localhost:8080 ，使用 `demo` 进入。不填写有效模型密钥也可以浏览已有作品。

如需让用户在项目设置中保存自定义模型 API Key，请在首次使用前设置稳定的
`MODEL_CREDENTIAL_KEY`。该值用于加密数据库中的模型密钥，部署后修改会使旧密钥无法解密；
未设置时会从 `JWT_SECRET` 派生，以兼容本地开发。设置页支持 OpenAI 兼容接口与
Anthropic Messages 原生接口，请按服务商要求填写包含版本前缀的基础地址。

| 本机地址 | 服务 |
|---|---|
| 127.0.0.1:8080 | Go 网关、前端、API、SSE |
| 127.0.0.1:5432 | PostgreSQL + pgvector |
| 127.0.0.1:6380 | Redis：限流、锁、心跳、SSE 事件 |
| 127.0.0.1:5672 | RabbitMQ：任务、延迟重投、死信 |
| 127.0.0.1:15672 | RabbitMQ 管理界面，演示账号 aiink/aiink |

Python API 的 8100 端口仅在容器网络内开放。所有宿主机端口默认绑定回环地址。不要将这份演示配置原样开放到公网。

首次启动由 `docker/initdb/01-roles.sql` 创建非超级用户 `aiink_app`；API 启动时运行 `aiink init`，创建表、RLS、必要补丁和演示数据。已有数据库升级目前使用幂等补丁，尚无完整的 Alembic 版本迁移链。

```bash
docker compose logs -f aiink-api aiink-worker aiink-gateway
docker compose down       # 停止应用，保留作品数据卷
docker compose up -d --build
```

`docker compose down -v` 会删除作品数据，不能用作日常重启。

## 本机开发

```bash
docker compose up -d --wait aiink-pg aiink-redis aiink-rabbitmq
python -m pip install -e '.[dev]'
aiink init
```

在不同终端运行 `aiink-api`、`aiink-worker`、`cd gateway && go run ./cmd/gateway`、`cd web && npm ci && npm run dev`。本机连接 Compose RabbitMQ 使用 `.env.example` 中的 `amqp://aiink:aiink@localhost:5672/`；容器内使用服务名 `aiink-rabbitmq`。不要混用 guest 凭据。

前端开发地址 http://localhost:5173 ，通过 Vite 代理访问网关。

## 自动化回归（独立测试数据）

```bash
SKIP_IMAGES=1 bash scripts/ci-local.sh
# 加上镜像构建：
bash scripts/ci-local.sh
```

使用 Bash 环境（Windows 可用 Git Bash）。脚本通过 `docker-compose.test.yml` 启动 `aiink-test` 专用项目，不停止开发 worker、不挂载开发数据卷。测试结束后清理临时容器及其卷。测试数据库为 tmpfs，停止或删除容器后不保留数据。

| 测试环境变量 | 值 |
|---|---|
| DATABASE_URL | postgresql+psycopg://aiink_app:aiink@127.0.0.1:15432/aiink |
| ADMIN_DATABASE_URL | postgresql+psycopg://aiink:aiink@127.0.0.1:15432/aiink |
| REDIS_URL | redis://127.0.0.1:16380/0 |
| REDIS_ADDR | 127.0.0.1:16380 |
| AMQP_URL | amqp://aiink:aiink@127.0.0.1:15673/ |

脚本依次执行 Python 语法检查、初始化、契约导出差异检查、Python 全量回归、Go vet/测试、前端 lint/交互测试/构建。模型和 embedding 使用替身，不产生真实模型费用。Go 的 SKIP 会被门禁判为失败。

改动 API 后先执行 `aiink contract export` 并提交 `spec/api-openapi.json`，再运行检查；未提交的契约变化会被 `git diff --exit-code` 拦截，这是预期行为。

GitHub Actions 的 Python 和 Go job 均提供 RabbitMQ。PG 业务角色在 checkout 后通过 SQL 创建，避免服务容器早于 checkout 导致初始化脚本缺失。

国内镜像覆盖示例（只用于测试基础设施）：

```bash
PG_IMAGE=docker.m.daocloud.io/pgvector/pgvector:pg16 \
REDIS_IMAGE=docker.m.daocloud.io/library/redis:7-alpine \
RABBITMQ_IMAGE=docker.m.daocloud.io/library/rabbitmq:3.13-management \
SKIP_IMAGES=1 bash scripts/ci-local.sh
```

## 上下文与向量配置

`REQUEST_TOKEN_BUDGET=64000` 控制完整请求的估算输入上限，包含正文、提示词、记忆和工具 schema/返回。`RECALL_TOKEN_BUDGET=12000` 单独限制可选记忆的增量，不能充当整章正文预算。提示词另预留 1000 tokens 给纠错消息，write/audit 还按实际工具 schema 大小预留；发送前按模型注册表的上下文窗口减输出预留再次限制。估算不是精确 tokenizer，真实 usage 另行记录。超出完整请求上限时仍明确失败，不截断正文或硬约束。

预算覆盖章节规划、写作、抽取、审核及章节节点的模型调用。整书规划、批次复盘、全局审计等独立调用尚未统一到这一预算，不能宣称全系统所有请求均限制在 12k。

Compose 默认 `EMBED_ENABLED=0`，关闭向量腿但仍可使用关系与关键词召回。启用需将 Dockerfile 安装改为 `pip install .[ml]`，重建镜像，并给 API/worker 配置 `EMBED_ENABLED=1`、首次下载时 `EMBED_ALLOW_DOWNLOAD=1`，持久化模型缓存。本地 embedding 会额外占用内存与磁盘。开关只对新写入生效，存量事件的向量要另跑一次 `aiink embed-backfill --level event`（事实用 `--level world`，`--project` 可限定单本）补建；否则向量腿索引为空，召回静默退化成纯关键词，`recall_stats.vector_status` 会显示 `enabled_but_empty`。该命令幂等，可重复执行。

## 仍需完成的生产工作

正式身份验证、版本化数据库迁移、备份与恢复演练、集中监控和容量测试仍在后续范围。当前没有生产可用性或真实小说质量的保证；演示和面试应使用已验证的机制与测试结果描述能力。
