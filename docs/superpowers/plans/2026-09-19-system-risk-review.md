# Myink 系统安全与容量风险复核（只读）

日期：2026-09-19

## 范围与结论口径

本报告按 `docs/superpowers/specs/2026-09-19-readability-drafts-invites-design.md` 的风险范围，对当前工作树做静态、只读复核。没有运行测试、启动服务、访问真实模型或修改产品代码；唯一写入是本报告。因此，下文只记录能由代码或仓库内既有审计记录直接证明的结果，不把未验证的攻击链写成漏洞。

“既有”指风险在本次“可读性、建书草稿与邀请制注册”工作之前已经存在；“本次引入”指由这批功能改动新增。稳定范围内没有确认由本次功能引入的新风险。邀请码和草稿生命周期仍在并行实现，未用其瞬时工作树状态作结论。

## 执行摘要

| ID | 严重度 | 公网判断 | 来源 | 结论 |
|---|---|---|---|---|
| R1 | 高 | 公网上线阻断项 | 既有 | 已认证用户可保存或探测私网/回环 HTTP 端点；生成路径直接使用保存的地址，形成服务器侧网络访问面。 |
| R2 | 高 | 公网上线阻断项 | 既有 | 设定、大纲、文风和模型探针同步调用外部服务，绕过 RabbitMQ、业务配额、成本预算和 worker 并发控制。 |
| R3 | 高 | 成本控制阻断项 | 既有 | 日成本闸门只读已结算成本，不预留在途估算；多本书的已入队任务可以合计超过日预算。 |
| R4 | 高（可用性） | 存量升级阻断项；不是数据泄露 | 既有 | 品牌更名改变模型凭据派生命名空间；仓库内既有审计已确认两条存量密文只能用历史前缀解密。 |
| R5 | 高 | 公网上线阻断项 | 既有、明确的本地部署形态 | Compose 强制开发模式并使用本地固定基础设施凭据；API/worker 运行时仍持有管理员数据库 URL。当前回环绑定降低本机风险，但配置不能原样公网暴露。 |
| R6 | 中 | 认证用户滥用阻断项 | 既有 | `queued` 任务也允许 resume，且 resume 直接重复发布同一任务，不经过配额/预算/并发闸门，可制造重复消息和队列积压。 |
| R7 | 中 | 多用户可用性阻断项 | 既有 | 通用业务限流是进程级单一令牌桶，不按用户或来源分桶；一个高频客户端可消耗所有用户的共享突发额度。 |
| C1 | 中（容量） | 当前单 worker 下是容量选择，不是独立安全阻断项 | 既有 | 配置中的 `CONCURRENCY_LIMIT` 未被闸门使用；实际为“每用户每本书一个在途”，默认部署则由单 worker 全局串行。 |

建议的公网前置顺序是：先封闭 R1，再把 R2/R3/R6 纳入统一的任务准入与成本结算，随后处理 R4/R5；R7 和 C1 应在多用户容量验收前明确语义并压测。

## 已确认风险

### R1 — 保存的模型/MCP 地址允许服务器访问私网与回环地址

**证据**

- `src/myink/api/routes_settings.py:68-75` 的 `_clean_base_url` 只限制协议、userinfo、查询串和片段，没有拒绝私网或回环目标；`src/myink/api/routes_settings.py:107-140` 随后把该地址写入模型连接。
- `src/myink/api/routes_settings.py:88-104` 的探针守卫明确“刻意放行”私有段和回环地址，只拒绝链路本地、多播、未指定和保留地址。
- 账号级环境复用同一保存逻辑（`src/myink/api/routes_environment.py:86-96`），MCP 地址保存也只调用 `_clean_base_url`（`src/myink/api/routes_environment.py:57-68`）。
- 探针会实际请求该地址（`src/myink/providers/probe.py:70-81`、`src/myink/providers/probe.py:97-118`）；生成链会把保存的 `base_url` 直接交给 provider（`src/myink/providers/__init__.py:46-72`）。

**影响**

持有有效邀请账号的用户可以让 API/worker 对它们可达的私网或回环 HTTP 服务发起请求。端点路径受具体 provider/MCP 协议约束，因此本报告不声称可读取任意 URL；但服务器侧网络可达性和协议交互面已经成立。公网部署前需要统一的出站策略，而不能只在探针入口做部分 IP 分类。

**归属**：既有；本次草稿/邀请工作未引入。

### R2 — 同步 AI/探针端点绕过队列、配额、预算和 worker

**证据**

- 网关把模型探针、文风提炼、设定草稿和大纲草稿注册为普通受保护 HTTP 路由（`gateway/internal/handlers/router.go:69-88`），处理器直接转发 Python（`gateway/internal/handlers/tasks.go:305-352`、`gateway/internal/handlers/tasks.go:379-384`、`gateway/internal/handlers/tasks.go:417-438`）。
- 设定和大纲使用 180 秒同步转发窗口，普通同步转发为 30 秒（`gateway/internal/handlers/tasks.go:535-574`）。
- Python 路由在请求线程中直接调用模型：设定/大纲见 `src/myink/api/routes_book.py:332-355`、`src/myink/api/routes_book.py:420-451`，文风见 `src/myink/api/routes_style.py:62-86`。这些调用最终直接执行 provider `generate`（`src/myink/book_setup.py:13-34`、`src/myink/book_setup.py:46-64`、`src/myink/style_extract.py:93-114`）。
- provider 单次请求超时为 120 秒且最多重试三次（`src/myink/providers/anthropic.py:21-22`、`src/myink/providers/anthropic.py:102-105`、`src/myink/providers/anthropic.py:147-176`；`src/myink/providers/deepseek.py:27-29`、`src/myink/providers/deepseek.py:80-84`、`src/myink/providers/deepseek.py:124-184`）。探针自身也允许 20 秒（`src/myink/providers/probe.py:16`、`src/myink/providers/probe.py:70-75`、`src/myink/providers/probe.py:97-103`）。
- 这些路径没有调用 `queue.Enqueue` 或 `gates.lua`。成本累计只针对有 `task_id` 前缀的 worker 任务，并在 worker 终态执行（`src/myink/worker/processor.py:330-361`），因此同步调用不进入 Redis 日成本账。

**影响**

认证用户可以并发占用 API 的同步执行容量，并在业务日预算和写作配额之外触发真实模型调用。邀请制限制账号数量，但不限制单账号请求数，也不替代资源准入。公网部署前应将这些有成本/长耗时的调用纳入可计费、可并发限制的统一执行路径，或至少为它们提供独立的用户级并发与成本闸门。

**归属**：既有；本次草稿流程只是继续调用已有同步端点。

### R3 — 日成本检查不是预留，已入队任务可合计越过预算

**证据**

- `gates.lua` 读取 `rate:cost:{date}` 并检查 `cost_used + cost_est`（`gateway/internal/queue/gates.lua:32-35`），但通过后只扣章节配额、登记书数和在途任务（`gateway/internal/queue/gates.lua:37-47`），没有把 `cost_est` 写入成本键。
- 入队端把成本键和估算传给脚本（`gateway/internal/queue/enqueue.go:46-76`），同一用户不同书使用不同的在途集合（`gateway/internal/queue/enqueue.go:48-52`）。
- 实际成本直到 worker 终态或最后一次重试才累加（`src/myink/worker/processor.py:330-361`）。

**影响**

当不同书的多个任务在前一任务结算前入队时，它们都会基于相同的旧 `cost_used` 通过；各自估算单独不超限，但合计可以超过日预算。默认单 worker 只会延后执行，不能撤销已经通过的准入。日预算目前是事后观测加单次准入判断，不是严格上限。

**归属**：既有。

### R4 — 存量模型凭据派生命名空间不兼容

**证据**

- 当前凭据密钥派生固定使用 `myink:model-credentials:`，并在未设置独立 key 时回退到 JWT secret（`src/myink/providers/credentials.py:13-16`）。
- 仓库内已完成的只读数据审计记录：品牌更名提交 `c5a1714` 把前缀从 `aiink:model-credentials:` 改为当前前缀；两条现存密文用当前前缀可解密 0 条、用历史前缀可解密 2 条（`docs/PUBLIC-DEPLOYMENT-CHECKLIST.md:53-54`）。
- 配置示例已经提示在轮换 JWT 前固定独立 `MODEL_CREDENTIAL_KEY`（`.env.example:44-45`），但这不能自动修复已经因前缀变化失配的密文。

**影响**

依赖这两条存量连接的探针或生成无法取得 API key，属于升级可用性阻断；没有证据表明密文被泄露。本次只读审计没有重试解密或改写数据，结论引用上述既有审计记录。

**归属**：既有品牌迁移兼容问题，不是本次 JWT 轮换或邀请注册引入。

### R5 — 当前 Compose 是本地开发部署，运行时权限不适合公网

**证据**

- 仓库明确声明当前 Compose 仅适合本机验收，不能直接开放公网（`docs/PUBLIC-DEPLOYMENT-CHECKLIST.md:5`）。
- 网关和基础设施都绑定宿主机回环地址（`docker-compose.yml:16-17`、`docker-compose.yml:34-35`、`docker-compose.yml:53-55`、`docker-compose.yml:164-165`），但 API 和 worker 强制 `APP_ENV=dev`（`docker-compose.yml:79-85`、`docker-compose.yml:115-121`）。
- PostgreSQL/RabbitMQ 使用仓库可见的本地固定凭据，Redis 没有认证；这些当前由回环绑定和 Compose 内网缓解（`docker-compose.yml:11-17`、`docker-compose.yml:32-38`、`docker-compose.yml:48-55`）。
- API 与 worker 运行时都获得管理员数据库 URL（`docker-compose.yml:83`、`docker-compose.yml:119`）。`src/myink/db.py:35-37` 说明管理员连接可绕过 RLS；公网清单也要求把迁移与运行时分离（`docs/PUBLIC-DEPLOYMENT-CHECKLIST.md:29`）。
- 公网清单尚要求生产 HTTPS/安全头、基础设施凭据轮换、资源限制、监控和负载验收（`docs/PUBLIC-DEPLOYMENT-CHECKLIST.md:24-31`）。

**影响**

当前回环部署本身没有把这些端口公开；风险来自把同一配置直接搬到公网或扩大网络可达范围。固定凭据和无 Redis ACL 会在网络边界失守后扩大影响，运行时管理员 DB 凭据则会使 API/worker 进程被攻破后绕过数据库 RLS。它们是公网发布前的配置与权限阻断项，不应误报成当前本机已经被远程暴露。

**归属**：既有、明确选择的本地开发部署形态。

### R6 — resume 可绕过准入并重复发布 queued 任务

**证据**

- `_RESUMABLE` 包含 `queued`（`src/myink/api/routes_tasks.py:37-38`）。
- resume 把状态再次写为 `queued` 后直接调用 `amqp.publish`（`src/myink/api/routes_tasks.py:301-329`），没有调用网关的原子配额/预算/在途闸门。
- worker 有书锁和任务锁以避免并发执行同一任务（`src/myink/worker/processor.py:487-501`），但锁不阻止 HTTP 层先向 RabbitMQ 发布多份相同消息。

**影响**

任务所有者可在任务仍为 queued 时反复调用 resume，每次新增一条消息；也可在失败/暂停后不经过当天的成本与配额检查重新触发。worker 幂等降低重复写入风险，但不能消除消息积压和调度开销。

**归属**：既有；当前工作树新增的是任务归属校验，不是 `queued` 可续跑或直发行为。

### R7 — 通用限流是所有用户共享的单桶

**证据**

- `limiter.Middleware` 在中间件构造时只创建一个 `rate.NewLimiter`（`gateway/internal/limiter/limiter.go:12-20`）。
- 该单桶挂在整个路由器上（`gateway/internal/handlers/router.go:21-28`）。认证端点另有 Redis 来源限流，但普通业务端点没有对应的用户级桶（`gateway/internal/handlers/auth.go:95-121`）。

**影响**

任何一个高频客户端都能消耗全站共享的令牌/突发额度，使其他用户收到 429。全局桶可保留为总保护，但不能承担多租户公平性。邀请制减少账号数量，不改变单账号争抢共享桶的事实。

**归属**：既有。

## 容量选择（不单独定为当前安全漏洞）

### C1 — `CONCURRENCY_LIMIT` 配置未参与实际闸门；默认 worker 全局串行

- 配置声明并默认 `CONCURRENCY_LIMIT=1`（`gateway/internal/config/config.go:21-29`、`gateway/internal/config/config.go:80-85`），但 `queue.Enqueue` 传给 Lua 的参数没有该值（`gateway/internal/queue/enqueue.go:70-76`）。
- Lua 只检查 `rate:inflight:{uid}:{pid}` 的集合大小是否大于 0，即每用户、每本书各一条，而非每用户总并发（`gateway/internal/queue/gates.lua:3-5`、`gateway/internal/queue/gates.lua:28-31`）。
- 默认 Compose 只有一个 worker 服务（`docker-compose.yml:111-138`），consumer 设置 `prefetch_count=1`（`src/myink/worker/consumer.py:127-154`），因此当前默认部署实际一次只处理一个队列任务。

这使配置名称与真实语义不一致：用户可在多本书上各占一个在途槽位并形成队列，但单 worker 串行执行。当前它首先是吞吐/排队选择；若未来横向扩 worker，多本书会真正并行，而 `CONCURRENCY_LIMIT` 仍不会限制用户总并发。上线前应明确想要的是全局 worker 容量、每用户并发，还是每书串行，并据此做容量验收。

## 隔离边界复核

本次检查的稳定访问路径没有发现可确认的跨用户读取或写入绕过：

- 业务路由统一挂 JWT 和每请求 session introspection（`gateway/internal/handlers/router.go:34-43`；`gateway/internal/handlers/auth.go:20-51`）。
- 写作入队先向 Python 做项目归属/ready 检查，SSE 也先检查任务访问权（`gateway/internal/handlers/tasks.go:99-112`；`gateway/internal/handlers/sse.go:34-40`）。
- worker 在锁和写入前校验消息中的 user/project/task 归属（`src/myink/worker/processor.py:456-476`）。
- 带 `project_id` 的业务表启用并强制 RLS；`users`、`projects`、`tasks`、`agent_runs` 仍依赖应用层边界（`src/myink/db.py:83-120`）。
- 草稿写作当前同时有网关项目状态检查和 worker 写序守卫（`src/myink/api/routes_tasks.py:65-72`；`src/myink/worker/processor.py:121-140`）。这只是确认双保险存在，不是对仍在编辑的完整草稿生命周期验收。

这里的关键部署前提仍是：Python API 不得成为公网入口，因为内部路由信任网关写入的 `X-Myink-User`；当前 Compose 没有向宿主机发布 Python 端口，公网清单也明确要求保持内部网络隔离（`docs/PUBLIC-DEPLOYMENT-CHECKLIST.md:24-25`）。这是一条必须保持的边界，而不是已发现的当前绕过。

## 明确未判断的项目

- 邀请码摘要、过期/撤销、并发一次性兑换和注册失败回滚：实现仍在并行编辑，避免把瞬时 diff 当最终结果。
- 草稿迁移分类、提案恢复、完整状态转换和前端“待完成”导航：同上；本报告只确认当前网关/worker 有双重 ready gate。
- 字体、主题对比度和 Windows 实机可读性：属于另一路验收，不是本安全/容量静态审计。
- 浏览器 localStorage 令牌/XSS、生产 CSP、反向代理可信 IP、TLS/HSTS：仓库公网清单已列出，但本次没有生产代理配置或浏览器动态测试，未重复判定细节。
- DNS 重绑定、HTTP 重定向、外部 MCP/provider 的具体行为：没有发出网络请求，也不据未运行行为扩大 R1 的结论。
- 真实吞吐、线程池上限、RabbitMQ/Redis/PostgreSQL 资源水位和模型供应商额度：任务明确禁止启动服务或压测，因此不提供虚假容量数字。
- `.env` 中的实际秘密值、外部基础设施 ACL 和云防火墙：未读取或验证秘密，也没有外部环境证据。
- 存量密文修复与生产密钥轮换：只引用已有只读审计，没有解密、改写或迁移任何凭据。
