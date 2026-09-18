# 上线方案：账号鉴权与模型密钥隔离

本文是**公网上线**时才做的安全方案，不改当前本地 MVP 行为。本地仍可无密码进 `demo`；`APP_ENV=prod` 已拒绝演示签发。落地时按本文替换，不要把 Compose 演示配置原样暴露公网。

对齐现有隔离口径（`src/myink/db.py` §14.1）：事务级 `SET LOCAL`、FORCE RLS、未设上下文空集、应用角色 `NOBYPASSRLS`、网关 JWT + 应用层第二道门。密钥与密码的租户粒度是 **`user_id`**，不是 `project_id`。禁止把现有 `app.tenant_id = project_id` 策略套到 `users` 或密钥行上。

---

## 1. 当前缺口（必须当缺陷看）

| 面 | 现状 | 上线后的真实风险 |
|---|---|---|
| 登录 | `POST /auth/token` 只收用户名即发 JWT | 扫到用户名就能进 |
| 用户表 | `users` / `projects` **故意无 RLS** | `myink_app` 可 `SELECT * FROM users` |
| 模型 Key | 密文在 `users.environment` JSON | 与根表同命运：一次查询拖走全站密文 |
| 加密 | Fernet，主密钥 `MODEL_CREDENTIAL_KEY` 或回落 `JWT_SECRET` | 回落等于两用一把钥匙；换密钥旧密文全废 |
| 登录限流 | 网关全站令牌桶（默认 20/秒） | 挡不住按用户名撞库 |
| Worker 读 Key | `new_session()` 按 `user_id` 取环境 | 忘写 WHERE 时库不拦 |

章节/记忆已经有 `SET LOCAL app.tenant_id` + `project_id` RLS。密钥现在吃的是根表例外，**比正文还弱**。

---

## 2. 威胁模型（本文覆盖范围）

要防：

1. 应用漏写 `WHERE`、SQL 注入、后台脚本：一次读出他人密钥行或密码哈希。
2. 连接池复用：上一请求的用户上下文漏到下一请求。
3. 在线撞登录接口（字典 / 用户名枚举）。
4. 数据库备份或 `SELECT` 拖库：密文/哈希被拿去离线撞。
5. 前端再次拿到明文 Key。

不在 v1 范围（单独立项）：SSO/OIDC、MFA、硬件 KMS、审计日志 SIEM、密钥轮转自动化、按书隔离 Key。

管理员用 `ADMIN_DATABASE_URL`（超级用户）仍绕过 RLS——这是现有 DDL 角色的已知坑。上线后该连接只给迁移机器，禁止业务进程使用。

---

## 3. 目标结构

```
浏览器
  → 网关（验 JWT / 登录限流 / 唯一公网入口）
    → Python API（127.0.0.1，信 X-Myink-User）
         ├─ 登录：SECURITY DEFINER 校验函数（应用角色不能直接 SELECT 哈希）
         ├─ 根表 users/projects：仍无 RLS，只列自己的书
         ├─ 凭据表：SET LOCAL app.user_id + FORCE RLS
         └─ 书内表：维持现有 SET LOCAL app.tenant_id + project_id RLS

Worker 生成
  → 先 SET LOCAL app.user_id（书主人）
  → 再 SET LOCAL app.tenant_id（书 id）
  → 解密 Key、跑模型
```

两套 GUC 并存，互不替代：

| GUC | 谁设置 | 策略过滤 |
|---|---|---|
| `app.user_id` | `user_session(user_id)` | `user_credentials.user_id` |
| `app.tenant_id` | `tenant_session(project_id)` | 带 `project_id` 的业务表 |

未设置对应 GUC → 该侧策略空集（`NULLIF(current_setting(...), '')` 判空，与现网坑 2 相同）。

---

## 4. 密钥存储

### 4.1 拆出根表

删除「密钥住在 `users.environment`」作为上线形态。新建：

```text
user_credentials
  id            uuid pk
  user_id       uuid not null references users(id) on delete cascade
  name          text
  protocol      text        -- openai | anthropic
  base_url      text
  model         text
  api_key_encrypted text not null
  created_at / updated_at
```

路由表（planner/writer/…）可同表附属列，或 `user_model_routes(user_id, role, credential_id | builtin_model)`。扫榜 MCP 覆盖（无模型 Key）可留 `users.environment` 的 `rankings` 段，或同样按 `user_id` 进配置表并套同一 RLS。

`users.environment` 上线迁移：一次性把 `models` 迁入新表后清空该键，避免根表继续囤密文。

### 4.2 用户级 RLS（与书隔离同一套手法）

```sql
ALTER TABLE user_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_credentials FORCE ROW LEVEL SECURITY;

CREATE POLICY user_isolation ON user_credentials
USING (
  NULLIF(current_setting('app.user_id', true), '') IS NOT NULL
  AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
);
```

应用侧新增 `user_session(user_id)`：`set_config('app.user_id', :uid, true)`（`is_local=true` = `SET LOCAL`）。事务提交/回滚后恢复，连接池不能串用户。

环境配置 API、探针复用已存 Key、`make_chain` / worker 读 Key：**只许**走 `user_session`。禁止 `new_session()` 碰 `user_credentials`。

### 4.3 列权限（堵住「暴力查表」）

即使将来有人用无 GUC 的连接，也不该让业务角色直接扫密文列：

```sql
REVOKE ALL ON user_credentials FROM PUBLIC;
GRANT SELECT (id, user_id, name, protocol, base_url, model)
  ON user_credentials TO myink_app;
-- api_key_encrypted：不 GRANT SELECT 给业务角色
```

解密走 owner 定义的函数，例如：

```text
myink_decrypt_credential(credential_id uuid) RETURNS text
SECURITY DEFINER
-- 函数体内再断言 user_id = current_setting('app.user_id')
-- 用库外主密钥或仅返回已加密行给应用层 Fernet（二选一，见 4.4）
```

推荐仍在**应用层 Fernet 解密**（主密钥不进 Postgres），函数只负责「校验 RLS 上下文后返回该行密文」；应用再解。这样 PG 超级用户备份里仍是密文，主密钥只在 API/Worker 环境变量。

### 4.4 加密

- 独立 `MODEL_CREDENTIAL_KEY`（`openssl rand -base64 48`），**禁止**再回落 `JWT_SECRET`。
- 生产启动：两值任一空或相等 → 拒绝启动。
- 算法维持现有 Fernet（`credentials.py`），密钥用 SHA-256 派生，与现网兼容迁移。
- 前端 GET 只回 `has_api_key`，永不回明文或密文。
- 轮转：新密钥加密新写入；旧密文用旧密钥解一次再重加密。轮转是独立任务，v1 只要求「主密钥不可变或可双密钥过渡」。

Worker 必须能在用户离线时调模型，因此**不能**用登录密码当唯一包装密钥（用户不在线就解不开）。密码只护登录；模型 Key 用服务端 KEK。

---

## 5. 账号与登录

### 5.1 密码本身

- `users` 增加 `password_hash`（Argon2id）。本地 demo 用户可空；`APP_ENV=prod` 空哈希禁止登录。
- **Pepper** 放环境变量 `PASSWORD_PEPPER`，不进库。哈希输入为 `pepper || password`（或 HMAC(pepper, password) 再 Argon2id）。拖库的人没有 pepper，不能直接拿哈希去撞。
- 每用户盐由 Argon2id 自带。参数：memory ≥ 64 MiB，iterations ≥ 3，与 OWASP 当年建议对齐后写入配置。

### 5.2 禁止业务角色扫哈希表

登录发生在签发 JWT **之前**，还没有可信 `app.user_id`，所以不能靠用户 RLS 护 `password_hash`。

做法：应用角色 **REVOKE SELECT (password_hash)**。校验只通过：

```text
myink_verify_password(username, password_pepper_hash_input) RETURNS uuid
SECURITY DEFINER
```

- 用户不存在也走一遍固定耗时的假哈希（防枚举）。
- 只返回 `user_id` 或空，不返回哈希。
- 函数属主为表 owner；`myink_app` 只有 `EXECUTE`。

「暴力查表」在库这一层被关掉：`SELECT password_hash FROM users` 对业务角色应失败。

### 5.3 在线撞库

网关对 `POST /api/v1/auth/token` **单独**限流，不复用全站 20/秒：

- Redis：`auth:fail:ip:{ip}`、`auth:fail:user:{username}`。
- 建议：同一 IP 15 分钟 20 次失败 → 429；同一用户名 15 分钟 10 次 → 锁定 15 分钟。
- 成功登录清该用户失败计数。
- 响应统一「用户名或密码错误」，不区分 404/403。
- 生产必须带密码字段；无密码或演示签发路径保持 `DEMO_LOGIN_DISABLED`。

JWT 维持现状：HS256、`sub=user_id`、短 TTL、`iss=myink`。网关验签后透传 `X-Myink-User`。生产 `JWT_SECRET` 独立、足够长，与 `MODEL_CREDENTIAL_KEY` / `PASSWORD_PEPPER` 三者互不相同。

### 5.4 注册（若上线开放注册）

另文可以补：用户名/邮箱唯一、密码强度、注册同样按 IP 限流。v1 也可以只运营导入用户、不开放自助注册。

---

## 6. Worker / 生成路径

任务 payload 已有 `project_id`。生成前先查书主人（`projects` 根表，应用层校验），然后：

1. `user_session(owner_id)` 取凭据并解密  
2. `tenant_session(project_id)` 读写章节/记忆  

两段可以同进程顺序执行，**不要**在一个未设 `app.user_id` 的会话里碰凭据表。队列消息里不要带明文 Key。

---

## 7. 环境变量（生产必填，互不相同）

```text
APP_ENV=prod
JWT_SECRET=                 # 仅签/验 JWT
MODEL_CREDENTIAL_KEY=       # 仅 Fernet 模型 Key
PASSWORD_PEPPER=            # 仅密码哈希
DATABASE_URL=               # myink_app，NOBYPASSRLS
ADMIN_DATABASE_URL=         # 仅迁移机
```

用户模型 Key 只走账号环境配置 / 凭据表，不写进进程 `.env`。

---

## 8. 迁移步骤（有活库时）

1. 补列 / 建 `user_credentials`，幂等 `ensure_*` 或 Alembic（上线应开始用版本迁移，不再只靠 init 补丁）。
2. `ENABLE/FORCE RLS` + `user_isolation`；`REVOKE` 密文列与 `password_hash`。
3. 建 `myink_verify_password` / 密文读取函数。
4. 后台任务：读旧 `users.environment.models`，写入新表，清空旧键。
5. 发布 API/Worker/网关（登录改密码、环境配置改走 `user_session`）。
6. 确认 `SELECT api_key_encrypted FROM user_credentials` 以 `myink_app` 无 GUC 执行返回 0 行或权限错误。
7. 再打开公网。

回滚：保留旧 JSON 一版备份至迁移窗口结束；函数与 REVOKE 回滚需 DDL，先在预发演练。

---

## 9. 验收（上线门禁）

用 `myink_app` 连接、**不** `SET LOCAL`：

- `SELECT * FROM user_credentials` → 0 行或无权限。
- `SELECT password_hash FROM users` → 无权限。
- `SELECT api_key_encrypted FROM user_credentials` → 无权限。

设了错误的 `app.user_id`：只能空集，不能读到别人行。

设了正确 `app.user_id`：只能看到自己的连接元数据；密文仅能经规定函数拿到。

登录：

- 错密码 / 错用户名耗时同量级，文案相同。
- 超限 429，不打库。
- 无密码请求在 `APP_ENV=prod` 失败。
- 单测覆盖：漏设 GUC 读凭据为空；`user_session` 下 `make_chain` 仍能解自己的 Key。

前端：保存 Key 后刷新看不到原文；另一个账号登录看不到该连接。

---

## 10. 与本地 MVP 的关系

| | 本地（现在） | 上线（本文） |
|---|---|---|
| 登录 | 用户名即身份 | Argon2id + pepper + 校验函数 + 登录限流 |
| 密钥位置 | `users.environment` | `user_credentials` + 用户 RLS |
| 读密钥 | `new_session` + user_id | 仅 `user_session` |
| 主密钥 | 可回落 JWT | 独立且启动校验 |
| 网关 | 全站限流 | 登录单独计数 |

未落地本文前，产品口径保持 README / `docs/DEPLOY.md`：本地演示，不是公网账户系统。

---

## 11. 建议工期切片

1. `user_session` + 凭据表 + RLS + REVOKE + 从 `environment` 迁出模型 Key（安全收益最大，不先碰登录 UX）。
2. 密码列 + 校验函数 + 登录限流 + 前端登录框。
3. 启动校验三密钥、预发按 §9 跑一遍、再切 `APP_ENV=prod`。
