"""The live verification helper must reject development services before any I/O."""

from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("redis_url", [
    "redis://localhost:6380/0", "redis://127.0.0.1:16380/1",
    "redis://192.0.2.10:16380/0",
])
def test_smoke_rejects_non_test_redis_before_http(monkeypatch, redis_url):
    namespace = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "verify-auth-isolation.py"))
    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "settings", SimpleNamespace(
        app_env="test", database_url="postgresql+psycopg://myink_app:myink@127.0.0.1:15432/myink",
        redis_url=redis_url,
    ))

    def forbidden_http(**kwargs):
        raise AssertionError("HTTP reached before validating dedicated Redis")

    monkeypatch.setattr(namespace["httpx"], "Client", forbidden_http)
    with pytest.raises(RuntimeError, match="dedicated local test Redis"):
        main()
