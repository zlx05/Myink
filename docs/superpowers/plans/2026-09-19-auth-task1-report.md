# Task 1 report: Python accounts and safe schema upgrade

## Result

Implemented the Python account boundary without changing or deleting legacy content.
The five private Python routes consumed by the gateway are now:

- `POST /internal/v1/auth/register` -> 201 `{token,user_id,username,tier,expires_in}`
- `POST /internal/v1/auth/token` -> 200 `{token,user_id,username,tier,expires_in}`
- `GET /internal/v1/auth/session` -> 200 `{user_id,username,tier}`
- `POST /internal/v1/auth/password` -> 200 `{ok:true}`
- `POST /internal/v1/auth/logout` -> 200 `{ok:true}`

Session, password-change, and logout validate the Bearer token directly instead of
trusting `X-Myink-User`. Existing business routes retain the private-gateway header
trust boundary.

## Changes

- Added `User.password_hash` (nullable for preserved legacy accounts) and
  `User.auth_version` (non-null, default 1).
- Added canonical usernames: trim, ASCII lowercase, 3-64 characters from
  `[a-z0-9_-]`. Non-ASCII input is rejected before lowercasing, so Unicode lookalikes
  cannot normalize into an ASCII account. Registration stores the canonical value and a PostgreSQL expression
  unique index enforces `lower(btrim(username))` under concurrent registration.
- Added standard-library scrypt password storage with `N=131072`, `r=8`, `p=1`,
  a random 16-byte salt, a 64-byte derived key, and `hmac.compare_digest`.
- Passwords must be 12-128 Unicode characters. Malformed or legacy password values
  fail closed.
- JWTs are HS256 and contain required `iss=myink`, UUID `sub`, `iat`, `exp`, and
  positive integer `ver`. Token introspection loads the current account and rejects
  missing accounts or version mismatches; tier comes from the current database row.
- All five auth operations fail closed with 503 before signing, verification, hashing,
  or account writes when the configured JWT secret is shorter than 32 UTF-8 bytes.
- A two-slot non-blocking semaphore bounds concurrent scrypt work (roughly 128 MiB per
  operation); saturation returns 429 instead of queueing unbounded memory-heavy work.
  Unknown-account login still verifies a fixed dummy scrypt hash to reduce account
  enumeration through response timing. Login password length is bounded before scrypt.
- Password changes and logout use guarded SQL updates on the observed
  `auth_version` (and the observed password hash for password changes), then increment
  the version atomically. A race loses with 401 rather than issuing two successful
  state transitions.
- Old `demo` and other passwordless accounts no longer log in. Unknown, passwordless,
  and wrong-password attempts share `401 {"detail":"INVALID_CREDENTIALS"}`.
- Auth success responses and credential failures include `Cache-Control: no-store`.
- Added the additive, transactional `ensure_user_auth_schema()` migration and the
  dedicated `python -m myink.cli auth-upgrade` command. This path does not invoke the
  legacy cleanup in `init`.
- Migration checks for duplicate `lower(btrim(username))` values before creating the
  index. It raises and rolls back the entire transaction, listing collisions for
  manual resolution; it never merges, renames, or deletes accounts or books.
- Added interactive `python -m myink.cli reset-password USERNAME`. It prompts twice,
  replaces only the password hash, increments `auth_version`, and preserves projects.
- Expanded focused auth/password tests for exact response shapes, hashing parameters,
  input rules, canonical uniqueness, passwordless login denial, strict JWT claims,
  direct Bearer introspection, atomic revocation, additive/idempotent migration,
  collision rollback, reset preservation, and the existing trusted-header boundary.

## TDD evidence

All commands used the requested isolated PostgreSQL/Redis/RabbitMQ URLs, `PYTHONPATH=src`,
`APP_ENV=test`, `EMBED_ENABLED=0`, and UTF-8 output.

Red run against the old implementation:

```text
python -m pytest <four focused auth cases> tests/test_passwords.py -q
11 failed in 6.25s
```

Failures were the expected missing registration route, password helper, additive
migration, password-reset command, and credential behavior. The later migration
collision test was also observed red before extraction of the transactional helper:

```text
1 failed in 5.81s
AssertionError: testable auth schema transaction is missing
```

Green focused verification, with a 32+ byte test-only JWT secret and pytest's unwritable
cache plugin disabled:

```text
python -m pytest tests/test_passwords.py tests/test_auth.py -q -p no:cacheprovider
36 passed in 25.02s
```

Additional red/green cycles covered Unicode-to-ASCII username folding, missing scrypt
capacity bounding, and weak JWT configuration. The claim-validation regression now
mutates tokens for a real registered account/current version, so missing `iss`, `iat`,
`exp`, or `ver` cannot be masked by a nonexistent subject.

`git diff --check` completed with exit code 0 for all Task 1 source and test files.

An earlier broader contract-focused run produced `82 passed, 2 failed`. Both were
cross-task contract integration items rather than auth behavior failures:

1. `tests/test_api_contract.py` assumes every successful operation uses response key
   `200`, while registration is intentionally and explicitly `201`.
2. `spec/api-openapi.json` has not yet been regenerated; the implementation plan assigns
   OpenAPI export to Task 4.

## Upgrade and recovery

Run the following against the intended database before starting the updated service:

```text
python -m myink.cli auth-upgrade
python -m myink.cli reset-password demo
```

If `auth-upgrade` reports duplicate normalized names, stop and choose distinct usernames
manually. Retrying after that correction is safe and idempotent. Do not use `myink init`
as the deployment upgrade command because it also contains historical cleanup and demo
seeding responsibilities.

## Concerns and follow-up

- The repository's default development JWT secret is only 24 bytes. The auth routes now
  refuse to operate with it (or any other secret shorter than 32 UTF-8 bytes), including
  in development. Deployment and local auth testing therefore require an explicit strong
  random `JWT_SECRET`. Existing installations that derived encrypted model credentials
  from the old JWT secret must preserve/configure `MODEL_CREDENTIAL_KEY` before rotating it.
- Gateway IP/account throttling remains necessary in addition to the new per-process
  two-slot scrypt capacity bound.
- The committed OpenAPI artifact and contract test's 201 handling remain Task 4 work as
  described above.
- No commit, push, deployment, destructive database cleanup, or default development
  database command was performed.
