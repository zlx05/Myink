# 可读性、草稿与邀请制实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. 不自动提交，不执行清理删除。

**Goal:** 清晰显示、可恢复的建书草稿和有限名额邀请注册。
**Architecture:** 复用主题变量、项目 API 和现有账户事务；增加项目建书状态及邀请凭据表；沿用网关和 worker 双层身份校验。
**Tech Stack:** React/TypeScript、FastAPI/SQLAlchemy/PostgreSQL、Go、RabbitMQ。
**Spec:** docs/superpowers/specs/2026-09-19-readability-drafts-invites-design.md

## Global Constraints

- 保留所有既有未提交修改，不提交、不删除数据，不使用真实 LLM 做测试。
- 使用独立测试 PostgreSQL 15432、Redis 16380、RabbitMQ 15673，不跑生产数据库测试。
- 邀请码至少 256 位随机性，默认一次兑换，已有用户登录不受影响。
- 新项目必须确认设定和有效大纲后才能生成章节；旧正文不改写。

## Review Focus

- 邀请事务并发及失败回滚，普通登录不重新兑换。
- 丢失响应/刷新导致重复建书或丢失确认状态。
- 部分 AI 提案失败时不能显示“全部生成成功”或提前解除忙状态。
- 直接调用 API 绕过草稿 UI 或跨用户恢复。
- Windows 低像素比及窄窗口、已保存的宋体偏好。

### Task 1: 邀请注册与发放命令

**Files:** 新增 src/myink/invitations.py、独立邀请模型、tests/test_invitations.py；修改 auth.py、cli.py、LoginPage/AuthContext/api 及认证测试/文档。
**Interfaces:** register(username, password, invitationCode) 发送 invitation_code；服务端在创建 User 的事务内原子核销，CLI create-invite 输出一次性明文。

- [ ] 增加缺失/无效/过期/重复/并发兑换以及失败回滚测试；运行并观察失败。
- [ ] 最小实现 32 字节随机令牌、SHA-256 摘要、截止时间/撤销状态与原子兑换。不存在开放注册回退。
- [ ] 前端增加注册专用邀请码字段，不保存到 localStorage；更新兼容性回归。
- [ ] 独立数据库运行相关及完整测试，保留结果。示例测试行为：`assert register_without_invite.status_code == 403`；已用码第二次兑换不能新增 User。

### Task 2: 字体清晰度

**Files:** web/src/lib/theme.ts/theme.test.ts、styles/global.css/tokens.css、AppearancePage 预览及说明。
**Interfaces:** --font-ui 始终系统无衬线，--font-editor 保留用户选择。

- [ ] 添加测试：`applyTheme('paper', undefined, {font:'song', fontSize:'m', chromeOpacity:100})` 后界面字体包含 sans-serif、正文包含 SimSun；观察失败。
- [ ] 分离字体栈，默认黑体正文，提高辅助字色并让输入控件继承字体；不强制页面缩放。
- [ ] 运行主题测试和完整 Web 检查；浏览器核对实际 computed style 与截图。

### Task 3: 建书生命周期与恢复

**Files:** Project 模型、db.py/cli.py、schemas/main/routes_book/routes_tasks、gateway tasks、worker processor；NewProjectPage、ProjectsPage、ProjectRail、WorkspacePage 与测试。
**Interfaces:** ProjectOut 包含 creation_status，草稿通过独立恢复路由访问；项目写作授权明确验证 ready。

- [ ] 测试先复现：创建项目状态 draft；只确认设定仍 draft；空大纲不放行；确认有效大纲后 ready；草稿生成拒绝。
- [ ] 增量 schema 和草稿上下文保存，不删除旧项目；同事务转换状态，已写作项目兼容。
- [ ] 前端草稿区、恢复向导和工作台跳转保护；删除跳过规划入口，修复并行忙状态与错误提示。
- [ ] 执行后端、网关、前端回归，更新生成契约。

### Task 4: 系统排查与交付

- [ ] 独立审查修改和相关系统风险，记录证据、级别及尚未修复项目。
- [ ] 全量测试并记录已有 xfail/warnings，禁止将跳过算通过。
- [ ] 检查队列和数据后增量更新应用容器；不重建数据卷。
- [ ] 发放少量一次性验收邀请，明确保存位置、注册方式和真实就绪程度。
