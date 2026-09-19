"""Additive project-creation migration preserves legacy book data."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

from sqlalchemy import text

import myink.db as db_module


class _ExistingConnectionEngine:
    """Expose Engine.begin() without committing the test's outer transaction."""

    def __init__(self, connection):
        self._connection = connection

    @contextmanager
    def begin(self):
        yield self._connection


def test_project_creation_upgrade_uses_active_schema_and_preserves_legacy_books(monkeypatch):
    """A hard-coded public schema would leave the empty legacy book writable."""
    schema = f"creation_migration_{uuid.uuid4().hex}"
    written_id, outlined_id, empty_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    chapter_id, outline_id = uuid.uuid4(), uuid.uuid4()
    engine = db_module.get_admin_engine()

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text("""
                CREATE TABLE projects (
                    id UUID PRIMARY KEY,
                    title TEXT NOT NULL,
                    current_chapter INTEGER NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE TABLE chapters (
                    id UUID PRIMARY KEY,
                    project_id UUID NOT NULL REFERENCES projects(id),
                    content TEXT NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE TABLE volume_outlines (
                    id UUID PRIMARY KEY,
                    project_id UUID NOT NULL REFERENCES projects(id),
                    outline JSON NOT NULL
                )
            """))
            connection.execute(
                text("""
                    INSERT INTO projects (id, title, current_chapter) VALUES
                    (:written_id, 'written', 1),
                    (:outlined_id, 'outlined', 0),
                    (:empty_id, 'empty', 0)
                """),
                {
                    "written_id": written_id,
                    "outlined_id": outlined_id,
                    "empty_id": empty_id,
                },
            )
            connection.execute(
                text("""
                    INSERT INTO chapters (id, project_id, content)
                    VALUES (:chapter_id, :project_id, :content)
                """),
                {
                    "chapter_id": chapter_id,
                    "project_id": written_id,
                    "content": "legacy chapter must survive unchanged",
                },
            )
            connection.execute(
                text("""
                    INSERT INTO volume_outlines (id, project_id, outline)
                    VALUES (:outline_id, :project_id, CAST(:outline AS JSON))
                """),
                {
                    "outline_id": outline_id,
                    "project_id": outlined_id,
                    "outline": json.dumps({"objective": "legacy outline"}),
                },
            )

            original_projects = connection.execute(text(
                "SELECT id, title, current_chapter FROM projects ORDER BY title"
            )).all()
            original_chapters = connection.execute(text(
                "SELECT id, project_id, content FROM chapters ORDER BY id"
            )).all()
            original_outlines = connection.execute(text(
                "SELECT id, project_id, outline FROM volume_outlines ORDER BY id"
            )).all()

            monkeypatch.setattr(
                db_module,
                "get_admin_engine",
                lambda: _ExistingConnectionEngine(connection),
            )

            db_module.ensure_project_creation()

            statuses = dict(connection.execute(text(
                "SELECT title, creation_status FROM projects ORDER BY title"
            )).all())
            assert statuses == {
                "empty": "draft",
                "outlined": "legacy_ready",
                "written": "legacy_ready",
            }
            assert connection.execute(text(
                "SELECT count(*) FROM projects WHERE creation_context::jsonb <> '{}'::jsonb"
            )).scalar_one() == 0
            assert connection.execute(text(
                "SELECT id, title, current_chapter FROM projects ORDER BY title"
            )).all() == original_projects
            assert connection.execute(text(
                "SELECT id, project_id, content FROM chapters ORDER BY id"
            )).all() == original_chapters
            assert connection.execute(text(
                "SELECT id, project_id, outline FROM volume_outlines ORDER BY id"
            )).all() == original_outlines

            first_upgrade = connection.execute(text(
                "SELECT id, creation_status, creation_context FROM projects ORDER BY id"
            )).all()
            db_module.ensure_project_creation()
            assert connection.execute(text(
                "SELECT id, creation_status, creation_context FROM projects ORDER BY id"
            )).all() == first_upgrade
            assert connection.execute(text("SELECT count(*) FROM projects")).scalar_one() == 3
            assert connection.execute(text("SELECT count(*) FROM chapters")).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM volume_outlines")).scalar_one() == 1
        finally:
            transaction.rollback()
