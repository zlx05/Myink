"""Application startup must not run destructive legacy cleanup during upgrades."""

from unittest.mock import MagicMock

from typer.testing import CliRunner


def test_init_does_not_run_legacy_schema_cleanup(monkeypatch):
    import myink.cli as cli
    import myink.db as db
    import myink.invitations as invitations
    import myink.seed as seed

    engine = MagicMock()
    monkeypatch.setattr(db, "get_admin_engine", lambda: engine)
    monkeypatch.setattr(cli.Base.metadata, "create_all", MagicMock())
    for name in (
        "enable_row_level_security", "ensure_chapter_versions", "ensure_genre_pack",
        "ensure_global_audit_reports", "ensure_memory_candidate_kinds",
        "ensure_project_creation", "ensure_storage_indexes", "ensure_unique_constraints", "ensure_user_auth_schema",
        "ensure_user_environment", "ensure_user_role", "ensure_user_tier",
    ):
        monkeypatch.setattr(db, name, MagicMock())
    cleanup = MagicMock()
    monkeypatch.setattr(db, "ensure_legacy_schema_cleanup", cleanup)
    monkeypatch.setattr(invitations, "ensure_invitation_schema", MagicMock())
    monkeypatch.setattr(cli, "create_demo_project", lambda: "demo-project")
    monkeypatch.setattr(seed, "create_sample_books", lambda: [])

    cli.init(no_seed=False)

    cleanup.assert_not_called()


def test_init_no_seed_runs_additive_setup_without_creating_demo_or_samples(monkeypatch):
    import myink.cli as cli
    import myink.db as db
    import myink.invitations as invitations
    import myink.seed as seed

    engine = MagicMock()
    monkeypatch.setattr(db, "get_admin_engine", lambda: engine)
    monkeypatch.setattr(cli.Base.metadata, "create_all", MagicMock())
    migrations = {}
    for name in (
        "enable_row_level_security", "ensure_chapter_versions", "ensure_genre_pack",
        "ensure_global_audit_reports", "ensure_memory_candidate_kinds",
        "ensure_project_creation", "ensure_storage_indexes", "ensure_unique_constraints",
        "ensure_user_auth_schema", "ensure_user_environment", "ensure_user_role",
        "ensure_user_tier",
    ):
        migrations[name] = MagicMock()
        monkeypatch.setattr(db, name, migrations[name])
    monkeypatch.setattr(invitations, "ensure_invitation_schema", MagicMock())
    demo = MagicMock(return_value="demo-project")
    samples = MagicMock(return_value=[])
    monkeypatch.setattr(cli, "create_demo_project", demo)
    monkeypatch.setattr(seed, "create_sample_books", samples)

    result = CliRunner().invoke(cli.app, ["init", "--no-seed"])

    assert result.exit_code == 0, result.output
    demo.assert_not_called()
    samples.assert_not_called()
    migrations["ensure_user_role"].assert_called_once_with()
