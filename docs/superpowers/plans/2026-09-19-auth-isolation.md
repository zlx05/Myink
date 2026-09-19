# Authentication and tenant isolation implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development; execute tasks with test-first implementation and independent review.

**Goal:** Replace demo login with real account authentication and verify isolation throughout writing.
**Architecture:** Preserve the existing internal-service trust boundary and project RLS; add password hashing, account token versions, gateway session validation and account-aware browser lifecycle.
**Tech Stack:** Python/FastAPI/SQLAlchemy/PostgreSQL, Go/Gin, React/TypeScript/Vitest.
**Spec:** docs/superpowers/specs/2026-09-19-auth-isolation-design.md

## Global Constraints

- Work only in this project. Do not delete user data or perform destructive Git/Docker operations.
- Preserve existing untracked files; do not commit, push, or deploy.
- All live tests target myink-auth-test ports 15432/16380/15673, with fake LLM/embedding.
- API shapes and password/username rules are defined in the spec.

### Task 1: Python accounts and safe schema upgrade

Files: src/myink/api/auth.py, api/schemas.py, models/project.py, db.py, cli.py; new password helper; tests/test_auth.py and focused credential/migration tests.
Produces the five auth endpoints in the spec, User.password_hash/auth_version, additive migration and interactive administrator password-reset command.

- [x] Add tests proving passwordless/invalid credentials fail, registration creates isolated normal users, duplicate case-insensitive names conflict, passwords are hashed, JWT is strict, logout/password changes invalidate old credentials.
- [x] Run those tests against the old implementation and record expected failures.
- [x] Implement secure hashes, input validation, transactional uniqueness/version updates, response models and migration. Do not run legacy cleanup on an existing database.
- [x] Run focused tests, include legacy account preservation and safe reset coverage; self-review and independent task review.

### Task 2: Gateway enforcement and writing isolation

Files: gateway/internal/handlers/{middleware,router,tasks,sse}.go, gateway/internal/pyapi/client.go and tests; Python task/worker files only if an isolation regression demonstrates a gap.
Consumes Task 1 GET /auth/session Bearer interface. Produces protected business/SSE routes, public registration/login proxies, authenticated password/logout proxies, rate limiting and strict JWT parsing.

- [x] Test missing issuer/expiry, forged headers, revoked sessions, auth proxying, cross-account task generation/control/events and failure-closed introspection.
- [x] Implement only changes needed to close observed gaps, plus narrow login/register throttling and body bounds.
- [x] Run go vet ./... and go test -count=1 -v ./... with isolated Redis/RabbitMQ, no SKIP; independent review.

### Task 3: Account UI and browser isolation

Files: web/src/context/AuthContext.tsx, lib/{api,token,apiError}.ts, pages/LoginPage.tsx, account page, router.tsx, components/ProjectRail.tsx, relevant cache/hooks and tests.
Consumes exact Task 1 public auth APIs through Task 2. Produces registration/password login, account password change, server logout and clean session changes.

- [x] Test credentials submitted correctly, registration validation, password changes, expiry/logout and switching accounts with stale async work.
- [x] Implement accessible forms using existing styles and session boundary/cache isolation.
- [x] Run npm run lint, npm test, npm run build; independent review.

### Task 4: Cross-layer verification and documentation

Files: tests/test_user_isolation.py, README.md, docs/DEPLOY.md, spec/api-openapi.json and minimal related deployment configuration.

- [x] Add real database two-user regressions spanning project roots, tenant tables, tasks and worker ownership, with fake providers.
- [x] Export OpenAPI, run full Python/Go/frontend checks, review all changed files and document limitations faithfully.
- [x] Record upgrade/reset instructions and internal network boundary. Leave changes uncommitted and retain test resources without broad cleanup.
