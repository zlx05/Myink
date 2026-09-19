# Task 3 前端账号与浏览器隔离报告

## 交付范围

- 把演示用户名登录替换为真实密码登录与注册，按网关契约提交
  `{username,password}`，使用服务器返回的规范化用户名、账号级别和到期时间。
- 新增账号页：展示当前账号、修改密码、说明本地管理员重设流程；不提供虚假的邮件找回。
- 退出改为调用服务器 `POST /auth/logout`。退出期间立即卸载受保护页面；请求 5 秒后中止。
  服务不可达时仍清除本机凭据，并在登录页明确提示“仅从本机退出”。
- 浏览器启动先调用 `GET /auth/session` 验证会话；已过期或 401 会话不挂载工作区。
  暂时无法连接服务器时保留凭据但关闭工作区，提供重试或本机退出。
- 会话改成单一原子 `myink.session` JSON。旧的三个认证键只用于一次性读取兼容，
  验证成功后迁移；清理认证态不会扫描或删除作品草稿、主题或其他 localStorage。
- 受保护 Outlet 使用 `userId:token` 作为 key。账号或 token 改变会卸载旧工作区；
  组件清理会关闭 SSE，API 层同时中止旧 token 的 fetch。
- 普通 API 与 SSE 都捕获发起时 token，响应、401、410 和 SSE 帧到达时复核 token。
  旧账号的迟到成功响应、401 或流事件不能写入或清除新账号状态。
- AuthContext 除处理 storage 事件外，登录完成、退出完成、改密、401 和到期定时器在写
  localStorage 前都会再次读取持久化 token。即使另一标签页的新 token 已写入、storage
  事件尚未送达，旧异步操作也只会采纳并验证新 token，不会覆盖或删除它。
- `activeWrite` 的 sessionStorage key 从项目级改为账号 + 项目级；
  章节草稿继续沿用原有账号 + 项目 + 章节 key。
- 新增账号导航、当前账号显示，并补齐
  `INVALID_CREDENTIALS`、`USERNAME_TAKEN`、`AUTH_SECRET_NOT_CONFIGURED`、
  `AUTH_CAPACITY_EXCEEDED`、`auth_rate_limited` 的中文提示。配置错误只说明管理员需
  配置安全的 `JWT_SECRET`，不展示任何 secret 值。

## TDD 红灯证据

实现前运行：

```text
npm test -- --run src/lib/api.auth.test.ts
Test Files 1 failed
Tests 1 failed
预期失败：/auth/token 请求缺少 password。
```

```text
npm test -- --run src/context/AuthContext.test.tsx src/pages/LoginPage.test.tsx +  src/lib/activeWrite.test.ts src/components/ProjectRail.test.tsx
Test Files 4 failed
Tests 8 failed | 1 passed
预期失败：无启动 session 校验、服务器规范账号未采用、无 storage 切换处理、
旧 unauthorized 会清状态、无注册表单/账号入口、activeWrite 未按账号隔离。
```

```text
npm test -- --run src/context/AuthContext.test.tsx
Tests 1 failed | 4 passed
预期失败：已认证会话到达 expiresAt 后仍保留。
```

```text
npm test -- --run src/lib/api.auth.test.ts src/pages/AccountPage.test.tsx
Test Files 2 failed
预期失败：旧 token 的成功响应在账号切换后仍 resolve；账号页尚不存在。
```

```text
npm test -- --run src/lib/sse.test.ts
Tests 2 failed | 15 passed
预期失败：SSE 401 未携带发起 token，A 账号迟到 401 会被当作 B 账号 unauthorized。
```

随后分别做最小实现并运行定向测试；最终定向隔离回归：

```text
npm test -- --run src/context/AuthContext.test.tsx src/lib/sse.test.ts +  src/pages/WorkspacePage.test.tsx
Test Files 3 passed
Tests 29 passed
```

## 最终验证

在 `web/` 运行：

- `npm run lint`：exit 0。保留 5 条 warning：AuthContext/router/ThemeContext 的
  fast-refresh 提示，以及既有 TaskHistory hooks、ssr_test 未使用参数提示；无 lint error。
- `npx tsc -b`：exit 0。
- `npm test`：32 个测试文件、204 项测试全部通过。
- `npm run build`：exit 0，681 modules transformed，产物生成成功。第一次沙箱内运行在
  Vite 清理 `web/dist/assets` 时遇到 Windows EPERM；按任务指示用批准的
  `npm run build` 升级权限重跑后通过。

## 已知提示与限制

- Vite 仍报告主 JS chunk 约 992 kB、超过 500 kB；这是现有整体打包结构的性能提示，
  不影响本任务认证正确性，未在本任务中扩展到无关代码拆分。
- 如果服务器退出请求网络失败，本机页面会立即退出并提示“仅从本机退出”；服务端 token
  无法在离线时主动撤销，只能等待其到期。重新连通后正常退出会撤销该账号所有会话。
- 未执行提交、推送、发布，也未改动 `web/` 以外的实现文件；本报告是任务明确允许的唯一
  文档例外。
