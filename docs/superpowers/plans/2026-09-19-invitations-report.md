# Invitation-gated registration implementation report

## Outcome

- New account registration now always requires a server-issued invitation. There is no configuration switch or fail-open path.
- Tokens use `secrets.token_urlsafe(32)` (32 random bytes) and the database stores only a SHA-256 digest.
- Invitations expire, can be revoked, and default to one redemption. The operator commands are `myink create-invite` (7-day default) and `myink revoke-invite <invitation-id>`.
- Registration performs a cheap eligibility preflight, then locks and revalidates the invitation row. Incrementing the redemption count, inserting the user, and constructing the authentication response happen before the same commit; username conflicts, response-construction failures, and other transaction errors roll back both user and redemption.
- Password login for existing users is unchanged. The browser shows the invitation field only in registration mode and never places it in local storage.

## TDD evidence

All Python commands below explicitly used the isolated services:

```text
DATABASE_URL=postgresql+psycopg://myink_app:myink@127.0.0.1:15432/myink
ADMIN_DATABASE_URL=postgresql+psycopg://myink:myink@127.0.0.1:15432/myink
REDIS_URL=redis://127.0.0.1:16380/0
AMQP_URL=amqp://myink:myink@127.0.0.1:15673/
APP_ENV=test RANKINGS_ENABLED=0 EMBED_ENABLED=0 PYTHONPATH=src
```

RED:

- `D:\mnist_data\ancanda\python.exe -m pytest tests/test_invitations.py -q`
  - Failed during collection with `ModuleNotFoundError: No module named 'myink.invitations'`, proving the backend feature was absent.
- `npm test -- --run src/lib/api.auth.test.ts src/context/AuthContext.test.tsx src/pages/LoginPage.test.tsx`
  - 4 expected failures: the API omitted `invitation_code`, AuthContext dropped the invitation argument, and LoginPage had no registration-only invitation field or missing-code validation.
- `D:\mnist_data\ancanda\python.exe -m pytest tests/test_invitations.py::test_response_construction_failure_rolls_back_user_and_redemption -q -p no:cacheprovider`
  - Failed because `_auth_response` raised after the transaction had already committed; the asserted-absent user still existed. This reproduced the late-response transaction gap.

GREEN (fresh final verification):

- `D:\mnist_data\ancanda\python.exe -m pytest tests/test_invitations.py tests/test_auth.py tests/test_cli_auth_safety.py -q -p no:cacheprovider`
  - Initial delivery: `37 passed in 20.81s`.
  - After closing the response-construction transaction gap: `38 passed in 23.69s`.
- `npm test -- --run src/lib/api.auth.test.ts src/lib/apiError.test.ts src/context/AuthContext.test.tsx src/pages/LoginPage.test.tsx`
  - `4` files, `19` tests passed.

Invitation coverage includes missing, unknown, expired, revoked, already-used, concurrent double redemption, plaintext non-storage, duplicate-username rollback/reuse, response-construction rollback/reuse, CLI defaults, and CLI revocation. Existing authentication tests now obtain explicit invitations for every registration; login/session/password/logout tests remain unchanged in behavior.

## Changed paths

- `src/myink/models/invitation.py` (new)
- `src/myink/models/__init__.py`
- `src/myink/invitations.py` (new)
- `src/myink/api/auth.py`
- `src/myink/cli.py`
- `tests/test_invitations.py` (new)
- `tests/test_auth.py`
- `tests/test_cli_auth_safety.py`
- `web/src/lib/api.ts`
- `web/src/lib/api.auth.test.ts`
- `web/src/lib/apiError.ts`
- `web/src/lib/apiError.test.ts`
- `web/src/context/AuthContext.tsx`
- `web/src/context/AuthContext.test.tsx`
- `web/src/pages/LoginPage.tsx`
- `web/src/pages/LoginPage.test.tsx`
- `README.md`
- `docs/AUTH.md`
- `docs/superpowers/plans/2026-09-19-invitations-report.md` (new)

## Remaining integration note

The focused OpenAPI drift check currently fails because `spec/api-openapi.json` does not yet match the integrated application schema. The root task is still adding project-creation endpoints, so the contract was deliberately not exported mid-edit; run `myink contract export` once those interfaces are final. Per root-task direction, no full Python/Web suite, build, container rebuild, production database operation, or real invitation issuance was performed here.
