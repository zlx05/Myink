"""PG checkpointer 的连接生命周期测试。"""

from __future__ import annotations


def test_checkpointer_normalizes_localhost_for_background_pool_workers():
    from myink.db_url import normalize_localhost_database_url
    from myink.workflow.checkpointer import _psycopg_conn_str

    assert _psycopg_conn_str(
        "postgresql+psycopg://user:pass@localhost:5432/myink?sslmode=disable"
    ) == "postgresql://user:pass@127.0.0.1:5432/myink?sslmode=disable"
    assert _psycopg_conn_str(
        "postgresql+psycopg://user:pass@myink-postgres:5432/myink"
    ) == "postgresql://user:pass@myink-postgres:5432/myink"
    assert normalize_localhost_database_url(
        "postgresql+psycopg://user:pass@localhost:5432/myink"
    ) == "postgresql+psycopg://user:pass@127.0.0.1:5432/myink"


def test_checkpointer_uses_health_checked_pool(monkeypatch):
    """长期缓存的 saver 必须通过池 checkout 健检，不能永久持有一个失效连接。"""
    import myink.workflow.checkpointer as module

    captured = {}

    class FakePool:
        @staticmethod
        def check_connection(connection):
            return connection

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def wait(self):
            captured["waited"] = True

    class FakeSaver:
        def __init__(self, connection):
            captured["connection"] = connection

        def setup(self):
            captured["setup"] = True

    module._build.clear()
    monkeypatch.setattr(module, "ConnectionPool", FakePool)
    monkeypatch.setattr(module, "PostgresSaver", FakeSaver)
    try:
        first = module.build_checkpointer()
        second = module.build_checkpointer()
    finally:
        module._build.clear()

    assert first is second
    assert captured["check"] is FakePool.check_connection
    assert captured["kwargs"]["autocommit"] is True
    assert captured["kwargs"]["prepare_threshold"] == 0
    assert captured["waited"] is True
    assert captured["setup"] is True
