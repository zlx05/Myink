# SDD ledger — plan: docs/superpowers/plans/2026-09-19-admin-observability.md

Task 4: complete. Live browser zlx login/7books (4ready+3draft)/admin overview verified; live admin tab retained. NewsmokeAPI/gateway stopped, fixtures/volumes preserved. Finalsixlivecontainers running/readinessok. Acceptance docs updated, no source changes after verified final builds. No commit/push/workspace cleanup. Earlier Latest/historical entries below describe prior checkpoints, not current pending work.

Task 4: final whole review APPROVED /root/admin_final_review, noCritical/Important. Minor deferred: overview cost_est title/currency misleading (actual model call estimate in yuan); recorded acceptance doc. Reviewer declined-to-judge: unrelated baseline dirty changes, documented public hardening, existing ciphertext/provider quality, absent historic prompts/invoice reconciliation, live actions notyetexecuted and duplicatefullsuites. Controller accepts those boundaries; live actions subsequently verified below.
Task 4: exact authorized --apply completed in unoptimized Python, hidden getpass input. Deleted olduser/project1/chapter1/task1/runs7, checkpointwrites41/blobs12/checkpoints12, exactRediskey1. Preserved original demoUUID and7books; 27protectedtable fingerprints unchanged; vipdemo unchanged. Newzlx/admin HTTPlogin/session passed, own7projects, users exactlyzlx+vipdemo, olddemo401, overview7projects44chapters372tasks653runs. Admin taskdetail/runs/rundetail/accesslogs and anonymous401/no-store verified. All six livecontainers running, APIhealthy, readyzok. No newbackup/volumedeletion/modelcall/commit/push.

Latest: Task3 complete with independent review and fix1 scoped approval. Corrected stale task status after detail refresh and non-degraded label; authenticated/unverified gate regression added. Root fresh full frontend38files/246pass,27.02s; lint5existingwarnings/buildchunkadvisory, exit0. Task4 final whole-source review /root/admin_final_review resumed after user interruption. Final gateway image f2ac5897 built and smoke updated; production accounts/containers still unchanged. Isolated browser sixviews/foreignchapter/context/taskflow/recallredaction/tasklessruns/audit verified,768px check reset, no browsererrorlogs. Latest production check remains old1/1/1/7 and retain7,27protectedtables; allthreequeues0ready/0unacked.

Ruling: 用户已有自主执行、不提问、不破坏的持续要求，跳过技能的反复批准/提交/删除步骤，书面方案自审后实施；当前checkout保留既有未提交认证/草稿代码，不建立缺失这些代码的新worktree。成本：所有改动留在当前工作树需后续人工整理提交。
Ruling (superseded): 原计划提升已有zlx；用户最新明确改为删除旧zlx及关联作品、不新建备份，原demo改名zlx并提升。成本：旧zlx数据无法通过本次新增备份恢复，原demo UUID与7本作品保留。
Ruling: 管理员权限实施为跨用户只读内容/观测，不新增破坏性管理或任意服务器命令；普通写作API保持owner-only。成本：未来确需代编辑/删除需独立设计。

## Preflight

| Tasks | Shared file/interface | Resolution |
|---|---|---|
| 1/2 | cli.py、models导出、require_admin | Task1完成后Task2接入；角色检查使用Bearer而非内部header |
| 1/3 | AuthContext/role、AccountPage | Task1先确定role，Task3只增加管理入口 |
| 2/3 | 管理API与分页类型 | Task2完成契约后Task3消费，不猜响应形状 |
| 1 | 密码混合规则/旧账户登录 | 仅新密码校验混合，登录只做长度与哈希验证 |
| 2 | 全量观测/资源与凭据 | 单记录有界截断并标记；脱敏后返回；不返回environment/model_routes |
| 3 | 高权限UI/实际安全 | UI隐藏仅便捷，服务端每请求校验角色 |
| 4 | 部署/已有数据 | 最新授权不备份，精确删除旧zlx；原demo保留UUID/内容摘要，使用无种子初始化 |

Ruling: Windows Bash入口失败（E_ACCESSDENIED），使用当前计划专属.local/admin-observability目录存放brief/report/review；不改系统配置。成本：脚本自动抽取改为手工维护。
BASE: c5a17148ff8d4accef738ca885b3362d4dfd88a6; current checkout has existing user changes, no commits planned.
Task 1: implementation delivered /root/admin_identity, awaiting /root/admin_identity_review. Focused Python54/frontend22 passed; production frontend build passed (chunk advisory). Root full frontend224 passed, lint0errors/5warnings. Root full Python760passed/5xfail/1 OpenAPI snapshot mismatch; normal contract export + focused58contract tests passed. Final full suite remains Task4.
Task 2: pending.
Task 3: pending.
Task 4: pending.

Task 1: review spec compliant, quality Needs fixes; important duplicated new-password validator in LoginPage/AccountPage. Fix round1 dispatched original implementer for shared helper + focused tests. Minor (deferred): add exact8-char API registration/change-and-login integration boundary test; Python validator boundary already covered. OpenAPI cannot-verify resolved by controller export +58 passing contract tests.

Task 1: fix round1/5 (1 addressed,0 open); passwordPolicy shared helper,16 focused tests/build passed, scoped re-review Approved. Minor exact8-char endpoint carried into Task2 fixtures.
Task 1: complete (uncommitted delta against c5a17148; independent review clean).
Task 2: dispatched /root/admin_observability_backend (gpt-6-astra/high); .local/admin-observability/baseline-2 snapshots backend/tests/gateway/spec after Task1; root real HTTP smoke prepared in .local/admin-observability/verify-admin-http.py, not run until isolated updated containers exist. No test suite currently running.

Ruling: 跨用户管理读复用现有管理数据库连接，严格Bearer管理员校验后开启只读事务；不放宽普通业务RLS，审计另走普通连接。成本：公网仍需拆分最小权限报表角色；应用持有管理凭据的既有风险未消除。

Task 2: implementation report received; correctness concern before review: existing plan/manual-plan/reflexion consumers read AgentRun.detail control fields and bounding all persisted detail could change later decisions. Returned to original implementer before task review; root full Python run already in progress on frozen initial delta, agent waits for it to finish before edits.
Ruling: 已有业务控制字段保持原有完整存储，新增观测快照单独限制大小，所有管理响应统一脱敏/有界。成本：历史业务产物的原有存储增长没有被这次快照限额覆盖；避免观测改变规划/审核决策优先于强制整个旧detail限额。

Task 2: business-control correction37focused passed, initial full Python775passed/5xfail/cacheprovider permission warning. Independent reviewer /root/admin_backend_review dispatched against task-2-corrected-review.diff. Test API image built and isolated18100 API initialized with no seeds; OpenAPI200/admin12paths, anonymous admin401/no-store. Formal containers/accounts unchanged.
Task 1: integration follow-up in progress original implementer: root full Go found stale fakePy auth fixture missing required role at handlers_test.go219, noSKIP. Production auth returns role; fix only test fixture and scoped contract test, then independent scoped review.
Task 3: baseline-3/web/src snapshot ready; not yet dispatched pending Task2 approval.

Task 1: integration fixture follow-up complete; independent scoped review Approved. Root full Go rerun exit0,80PASS lines,0FAIL,0SKIP; Go vet passed. Only test fixture gained required role.
Task 2: review Important URL scrubber quadratic scan (20k public-URL text ~7s); fix round1 reduced to~4ms,8focused passed. Scoped review: original addressed, new Important adjacent serialized URL consumes nextscheme and leaks secondcredential. Fix round2 dispatched original implementer; root fullPython fix1 stillrunning, wait before codechange. No API contract changes. New API image currentlyfix1, smokeAPI still initialcorrection; final rebuild/update required.

Task 2: fix round2/5 (all addressed,0open); independent URL delimiter windows;13focused passed,20k~8ms, scoped re-review Approved. Prior fullPython fix1 finished779passed/5xfail,zero warnings; final corrected fullsuite remains planned. Task1 auth dependency verified by Task1 independent reviews; contract export58test and isolatedAPI69paths/admin12 close review cannot-verify items.
Task 2: complete (uncommitted delta c5a17148, task review +2 scoped fix reviews; no openCritical/Important).
Task 3: next dispatch uses baseline-3/web/src, finalized task-3-brief.md; Task2 response contracts unchanged through corrections.

Task 3: dispatched /root/admin_panel (gpt-5.6-sol/high), implementation running; no backend source changes authorized.
Task 4: final Task2 corrected full Python784passed/5xfail in180.40s,zero warnings (-p no:cacheprovider); full Go exit0/zeroSKIP and vet alreadypassed on unchanged Go source. API fix2image build44340 running; gateway pre-panel image built, must rebuild afterTask3. Real HTTP smoke notrun yet; initial smokeAPI18100 healthy12adminpaths/401no-store.
Task 4: latest production read-only reassign-admin.py --check verified exact old1book/1chapter/1task/7runs, retained7books,27protectedtables. Actual accounts still unchanged. Secure password stdin verified with tty+getpass (no echo); plain non-tty stdin closes immediately, so actual apply must use tty/getpass and in-memory StringIO/runpy wrapper, never a password literal in command/file.

Task 4: fixed2APIimage built56c40c64, isolatedsmokeAPI+gateway updated. Real admin HTTP59checks passed; original auth/writing/SSE/password/logout HTTP39checks passed. Only dedicated15432/16380/15673 fixtures added; no production changes/deletions/model calls. Smoke readyz reports worker-degraded intentionally (no testconsumer); worker.entry import succeeds without running consumer.
Task 4: UI-only test fixture created admin-ui-59a27d7e445b46f797a13aad4a66af44 (UUID9574a3c6-11cc-47ff-a832-cb9d56215fa8, ordinary test password abcd1234), only isolatedDB. CUA hidden IAB tab2/adminAcceptanceTab logged into18080/projects; ownbooks empty despite other users' fixtures. Await new Task3 build/reload before panel verification. Original usertab8080 untouched.
