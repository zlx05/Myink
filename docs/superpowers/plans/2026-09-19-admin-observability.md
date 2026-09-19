# 管理员与全站观测 Implementation Plan

> **For agentic workers:** 使用 superpowers:subagent-driven-development；先TDD再实现，各任务独立审查。用户要求自主执行，保留当前已有未提交工作，不提交/推送/合并或删除。

**Goal:** zlx管理员能安全地查看全站写作内容与可追溯任务指标。
**Architecture:** 数据库独立role + 严格Bearer管理员依赖；独立分页管理API与安全观测；复用现有React布局显示。
**Tech Stack:** FastAPI/SQLAlchemy/PostgreSQL、Go、React/TypeScript。
**Spec:** docs/superpowers/specs/2026-09-19-admin-observability-design.md

## Global Constraints

- 保留未提交修改和无关数据；最新授权仅在部署阶段删除旧zlx及其1本作品，把原demo改名zlx。无需备份，不使用真实模型做测试，不提交/推送/合并。
- 普通用户继续隔离。role为user/admin，与tier分离；注册不能授予admin。
- 新密码8–128字符、ASCII字母+数字；登录兼容旧有效密码。密钥和指定密码不得写入代码/报告/镜像。
- 测试仅用PG15432、Redis16380、Rabbit15673，不并发运行共享服务全套测试。
- 管理读仅由真实Bearer+当前DB角色授权，清洗凭据，no-store，分页，访问留痕。

## Review Focus

1. 伪造role/header/localStorage不得获取admin数据，角色降低即时生效。
2. 删除旧zlx及其关联数据、原demo原地改名zlx；保留原demo UUID和7本作品，禁止合并账号或重建demo种子。
3. 批次任务仅匹配精确ID或ID+:ch前缀，分页/聚合不得串项目或遗漏。
4. 历史缺失信息和截断标记可见，费用估算/等待时间不能标成实测账单/纯运行时间。
5. 调试记录中嵌套凭据、HTML与超长文本不泄密、不执行、不无限增长。

### Task 1: 角色、密码与管理员命令

**Files:** passwords.py、models/project.py、db.py、api/auth.py/schemas.py、cli.py；web token/AuthContext/types/LoginPage/AccountPage；对应测试、AUTH文档。
**Interfaces:** User.role默认user；_AuthenticatedUser.role；require_admin依赖严格_authenticated_user；认证/会话响应role；StoredSession.role默认user兼容旧存储；CLI set-role <username> <user|admin>。

- [ ] 失败测试：validate_password('abcd1234')通过，纯字母/纯数字/7字符失败；登录旧有效密码不因混合规则失败；role字段默认user，注册传role=admin仍user，伪造用户头不能通过require_admin。
- [ ] 最小实现和增量ensure_user_role；init/auth-upgrade调用；CLI角色命令只修改既有用户并递增auth_version，重设密码继续交互式输入。
- [ ] 更新前端校验与role会话刷新；注册注明管理员可查看作品与调试内容；更改受新规则影响的测试夹具，不削弱测试。
- [ ] 专项与全套回归，报告RED/GREEN；独立任务审查。

### Task 2: 管理API与有界观测

**Files:** 新建api/routes_admin.py、api/admin_schemas.py、admin_observability.py、models/admin.py、tests/test_admin.py、tests/test_admin_observability.py；修改models导出、cli/init、api/main、workflow/nodes.py、gateway/admin.go/router.go及契约测试。
**Interfaces:** 完整路径和分页JSON见Spec；所有API依赖Task1 require_admin，响应role由Task1提供。AdminAccessLog由初始化创建；admin_observability提供可复用redact/有界capture函数供nodes记录和管理响应调用。

- [ ] 用真实隔离数据库夹具测试普通用户403/无Bearer401、管理员跨用户只读、分页统计、全文、batch节点匹配、访问审计；先观察失败。
- [ ] 实现管理读模型和数据库聚合，所有查询有界；网关仅固定管理GET路由转发Bearer，Python二次认证。不得改动require_owner或原任务写授权。
- [ ] 对嵌套secret/Bearer/API-key/超长数据建立失败测试；实现统一脱敏与截断；让新LLM调用、recall和plan_cast后实际上下文记录安全快照，已有产物保留合并。
- [ ] 测试观测不会改变生成输出与节点流程，旧detail=None可读取，管理接口不暴露用户环境；导出契约、全套验证、独立审查。

### Task 3: 管理面板

**Files:** 新建web/src/pages/AdminPage.tsx、AdminPage.module.css、AdminPage.test.tsx及lib/adminApi.ts；修改router/ProjectRail/AccountPage。沿用现有设计，不改普通作品布局。
**Interfaces:** 直接消费Task2 OpenAPI/固定分页JSON；useAuth().session.role区分入口，API权限由服务器决定。

- [ ] 失败测试：admin可浏览总览/用户作品/任务节点详情、普通账号不发管理请求并显示无权访问、点击内容后错误/加载/空态清楚。
- [ ] 实现可筛选分页的总览、作品章节只读阅读、任务列表与节点时间线及JSON详情，token/cost/耗时清楚标注，超长内容按需加载，支持手动刷新，不引入新依赖。
- [ ] role未验证或账号切换时不显示旧admin内容；低权限存储role伪造也只能收到服务器拒绝；注册页观测告知已由Task1实现。
- [ ] Web lint/test/build全套、独立审查。

### Task 4: 全面审查、部署与账号应用

- [ ] 新鲜全量Python/Go/Web及管理员真实HTTP隔离验收，记录xfail/warnings而非视作通过。
- [ ] 最终独立代码审查并修复关键问题。保留风险/历史缺失/截断说明。
- [ ] 不新建备份，检查队列空闲后更新API/网关/worker，不删除卷；精确删除旧zlx及关联作品，原demo改名zlx设置admin和指定密码并撤销旧会话。使用无种子初始化避免重建demo。
- [ ] 验证账号登录、面板、普通隔离、数据数量/正文摘要；只本机端口，保留回滚镜像和验收文档。
