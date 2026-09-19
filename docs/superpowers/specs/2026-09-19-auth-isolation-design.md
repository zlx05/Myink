# 账号认证与写作隔离

用户授权自主完成本项目登录、密码、注册与用户隔离，不提问，不执行破坏性操作。

## 设计

沿用 FastAPI + Go 网关 + React 与 PostgreSQL 项目 RLS。选择本地用户名密码认证；第三方 OAuth 需要外部账号配置，邮件找回需要邮件服务，均不引入。提供注册、密码登录、当前会话、修改密码、退出及本机管理员重设密码命令。账号密码使用标准库 scrypt（N=131072、r=8、p=1，随机盐、恒定时间比较），密码 12–128 字符；用户名去首尾空白、ASCII 小写规范化，3–64 位字母数字与下划线/短横线。

账号增加 nullable password_hash 与 auth_version。旧账号无密码时禁止登录，不自动认领或分配公共密码。迁移只增加字段/索引，重复规范化用户名时明确失败，绝不删改已有作品。注册在数据库唯一索引下处理竞争，永远创建 normal 用户及空环境。JWT 必须有 HS256、iss=myink、sub UUID、iat、exp、ver。退出与改密原子递增版本，使该账号所有旧 token 失效；改密成功要求重新登录。

公网入口始终为 Go 网关，Python/Redis/队列不能暴露公网。网关除 JWT 验签，还调用 Python GET /internal/v1/auth/session 校验用户与版本，每次业务请求失败关闭。内部 Python 现有 X-Myink-User 信任链只允许受信内网调用，公网身份头由网关覆盖。注册/登录限流、body 限制与 no-store 响应。凭证错误统一 401，不返回密码哈希。

## API

- POST /auth/register {username,password} -> 201 AuthResponse。
- POST /auth/token {username,password} -> 200 AuthResponse。
- GET /auth/session，Bearer -> {user_id,username,tier}。
- POST /auth/password，Bearer 与 {current_password,new_password} -> {ok:true}。
- POST /auth/logout，Bearer -> {ok:true}。
- AuthResponse: {token,user_id,username,tier,expires_in}。

## 隔离验收

两个独立注册用户互不可见作品/章节/世界观/记忆/版本/环境模型连接及密钥。伪造项目、任务、批次、章节或候选 ID 不得读取、修改、继续生成或订阅另一用户事件。任务进入队列及 worker 执行时 user_id/project_id 一致；checkpoint、检索与环境配置绑定所属项目。浏览器切换用户或退出时卸载旧工作区，取消旧请求/事件，缓存按账号隔离，旧响应不得覆盖新会话。

## 验证与约束

仅在 myink-auth-test 专属容器及 15432/16380/15673 测试端口运行集成测试，不运行默认开发数据库上的测试或清理，不调用真实模型。Python 全套 pytest、Go vet/test（不得静默 SKIP）、前端 lint/test/build、OpenAPI 导出及独立代码审查。保留已有 AGENTS.md、docs/BACKLOG.md、docs/team-workflow.md。所有改动留待用户审阅，不自动提交/推送/发布。
