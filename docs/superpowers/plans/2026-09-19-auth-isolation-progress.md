# SDD ledger — plan: docs/superpowers/plans/2026-09-19-auth-isolation.md

Base: c5a1714. Existing untracked files: AGENTS.md, docs/BACKLOG.md, docs/team-workflow.md.

Ruling: User explicitly requested autonomous execution without questions; design and implementation decisions proceed under that authorization, with no approval pauses or commits.
Ruling: Work in the supplied project checkout to keep the deliverable immediately reviewable and preserve its untracked instructions. No checkout/reset operations; changes remain uncommitted. Skill scratch/commit/delete steps are adapted to the user's no-destructive-operations requirement; reports remain in project documentation.
Ruling: Logout invalidates all sessions for that account via auth_version, a deliberate simple policy that costs users a new login on other devices.

| Check | Shared interface or consistency | Result |
|---|---|---|
| Tasks 1 / 2 | Bearer GET /auth/session and five auth API shapes | Matched in spec |
| Tasks 1 / 3 | AuthResponse, password limits and username normalization | Matched in spec |
| Tasks 2 / 3 | Public /api/v1 proxy, 401 invalidation | Matched |
| Tasks 1–4 | OpenAPI export and docs follow implementation | Task 4 owns generated output |
| Task 1 | Tests vs password/schema/session implementation | Consistent |
| Task 2 | Tests vs gateway and task ownership implementation | Consistent |
| Task 3 | Tests vs forms and lifecycle implementation | Consistent |
| Task 4 | Dedicated service ports and full validation | Consistent |

Test infrastructure: myink-auth-test, PostgreSQL 15432, Redis 16380, RabbitMQ 15673; new containers, no preexisting running containers. Images reused locally. No development volumes mounted.

## Verified backend/gateway results

- Task 1 implemented by accounts agent, independently reviewed by isolation_review: 36 password/auth/migration tests passed. Scrypt work is bounded to two concurrent operations; weak JWT secrets fail closed before writes/signing/verification. Invalid-claim tests use an existing account plus a valid-token control.
- Task 2 implemented by root, reviewed by isolation_review (Python) and gateway_review (Go). Added ownership checks before task controls, queue publication, worker processing and SSE. Initial negative tests reproduced missing checks. Reviewer-found transient owner-check failures now requeue messages; two regression tests were red then green.
- Gateway reviewer found active SSE sessions were not revoked and session/logout bodies were unbounded. Both fixed and re-reviewed. SSE tests passed for revocation (15.18s) and JWT expiry (5.03s, including Redis blocking read); no-store is asserted. Revocation detection may take approximately 20s, not instantaneous.
- Python full suite: 729 passed, 5 existing xfailed, 160.38s. Subsequent additive startup safety test was red (legacy cleanup called) then green (not called). Further dedicated Redis guard tests were added after ops review; final suite rerun pending.
- Go vet and full verbose tests passed, with live Redis/RabbitMQ and no SKIP. Contract tests passed. Subsequent SSE-expiry targeted test passed.
- Live integration: isolated API at 127.0.0.1:18100 and gateway at 18080, strong test signing secret and exclusive RabbitMQ prefix. `scripts/verify-auth-isolation.py` passed 32 real HTTP assertions: two registrations, password login, separate project lists, owned chapter/task/SSE reads, forged-header/cross-account read/control/generation/SSE denial, pending-task SSE denial, password change and logout invalidation. No real LLM calls. Only new test fixtures were written; none were deleted by the smoke script.
- Python syntax checks, 56-path OpenAPI regeneration/hash equality, and git diff whitespace check passed.

## Safety rulings and handoff notes

- Preserve existing `.env`, model credential encryption key, user database and all user-owned untracked files. No commit/push/deploy or destructive Git/Docker commands.
- Existing model credentials may use JWT_SECRET as their encryption input. Documentation requires pinning the old effective JWT secret to MODEL_CREDENTIAL_KEY before signing-key rotation; no automatic rotation was performed.
- `myink init` previously invoked destructive legacy table/column cleanup on every startup. Removed that automatic invocation for additive authentication rollout, with a red/green regression. The old helper remains untouched and is not called by startup.
- Ops review approved credential migration documentation but found the live smoke script needed a Redis-target guard. Added exact 127.0.0.1:16380/0 validation before any HTTP/I/O; its three negative tests first reproduced the gap.
- Frontend implementation and final integrated verification remain in progress. Test containers are retained; no broad cleanup of Docker resources is authorized.

## Final backend reruns

- Complete Python rerun after startup/Redis guard changes: **733 passed, 5 xfailed in 155.72s**. The xfails predate this task and remain explicitly not counted as passing.
- Redis-target guard was re-reviewed and approved; no remaining ops review findings.
- Gateway session version aligned to Python's positive integer contract (ver >= 1); new malformed-version regression first failed on zero, then passed. Final `go vet ./...` and `go test -count=1 -v ./...` passed with no SKIP (handlers 23.062s, queue 2.392s, SSE 1.132s).
- Live smoke API/gateway test listeners were stopped; dedicated Docker test services remain available without deleting resources.
- Frontend implementer reports 16 focused tests and lint passing; independent frontend review dispatched while full test/build run continues.

## Final disposition — all four tasks complete

- Frontend review found two delayed-response/session races: old SSE 401 incorrectly targeted the new token, and old async operations could overwrite newer persisted credentials before the storage event arrived. Both were fixed with captured-token checks and persistence checks, regression-tested, and independently re-reviewed as passing.
- Root independently reran frontend checks after those fixes: `npm run lint` exit 0 (5 warnings), `npm test` **32 files / 204 passed in 12.56s**, `npm run build` exit 0 (TypeScript + Vite, 681 modules). Warnings are fast-refresh/previous hook and unused-variable notices, plus the existing large JS chunk warning; no errors. No unrelated refactoring performed.
- Final backend evidence remains **733 passed / 5 existing xfailed**, Go vet and all verbose package tests passing with no SKIP, plus **32 live HTTP checks**. OpenAPI export is repeatable and syntax/whitespace checks passed. Docker image builds and a real browser visual inspection were not performed; frontend interaction tests use jsdom and backend live checks use HTTP.
- Test-helper shutdown required checking actual listener PIDs: Ctrl+C and Stop-Process did not stop them. After verifying the exact commands for our Python API PID 24888 and temporary Go gateway PID 30436, taskkill terminated only those two idle helpers; health requests no longer succeeded. No files, Docker volumes or user data were removed. The three dedicated `myink-auth-test` Docker services remain running.
- Normal repository on `main`, `.git` equals the common Git directory; no new branch/worktree. In accordance with the user's autonomous/no-destructive instruction, keep all changes uncommitted in place, with no merge/push/PR/deployment and no integration-choice question.
- Usage and safe upgrade guide: `docs/AUTH.md`. Existing `.env` and development database are unchanged; deployment requires a strong shared JWT signing key and password initialization for legacy accounts. Preserve model-credential encryption material when rotating JWT keys.
