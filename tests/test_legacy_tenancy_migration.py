"""Alembic migration 6 carries inline tasks.tenant_id / tasks.visibility into task_acl before dropping them."""

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "utils/db_migration/versions/6_drop_inline_tenancy_columns.py"


@pytest.fixture
def migration():
    spec = importlib.util.spec_from_file_location("drop_inline_tenancy", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_engine(columns=("tenant_id", "visibility")):
    engine = sa.create_engine("sqlite://")
    extra = "".join(f", {c} {'INTEGER' if c == 'tenant_id' else 'VARCHAR(16)'}" for c in columns)
    with engine.begin() as conn:
        conn.execute(sa.text(f"CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER{extra})"))
        if "tenant_id" in columns:
            conn.execute(sa.text("CREATE INDEX ix_tasks_tenant_id ON tasks (tenant_id)"))
    return engine


def _upgrade(engine, migration):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()


def _acl(engine):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT task_id, user_id, tenant_id, visibility, visibility_seq FROM task_acl ORDER BY task_id"))]


def _task_columns(engine):
    return {c["name"] for c in sa.inspect(engine).get_columns("tasks")}


def test_upgrade_carries_inline_tenancy_before_dropping(migration):
    engine = _legacy_engine()
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO tasks (id, user_id, tenant_id, visibility) VALUES "
            "(1, 2, 1, 'public'), (2, 2, 1, 'tenant'), (3, 2, 1, 'private'), "
            "(4, 5, NULL, 'public'), (5, 5, NULL, 'private'), (6, 5, NULL, NULL), (7, 5, 3, 'bogus')"))
    _upgrade(engine, migration)
    assert _acl(engine) == [
        (1, 2, 1, "public", 1),
        (2, 2, 1, "tenant", 1),
        (3, 2, 1, "private", 1),
        (4, 5, None, "public", 1),
        (7, 5, 3, "private", 1),  # unknown visibility is carried fail-closed
    ]
    assert not {"tenant_id", "visibility"} & _task_columns(engine)


def test_upgrade_keeps_existing_task_acl_rows(migration):
    from lib.tenancy.acl import ensure_acl_schema

    engine = _legacy_engine()
    ensure_acl_schema(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO tasks (id, user_id, tenant_id, visibility) VALUES (1, 2, 1, 'public'), (2, 2, 1, 'tenant')"))
        conn.execute(sa.text(
            "INSERT INTO task_acl (task_id, user_id, tenant_id, visibility, visibility_seq) VALUES (1, 2, 1, 'private', 4)"))
    _upgrade(engine, migration)
    assert _acl(engine) == [(1, 2, 1, "private", 4), (2, 2, 1, "tenant", 1)]


def test_upgrade_with_only_visibility_column(migration):
    engine = _legacy_engine(columns=("visibility",))
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO tasks (id, user_id, visibility) VALUES (1, 2, 'public'), (2, 2, 'private')"))
    _upgrade(engine, migration)
    assert _acl(engine) == [(1, 2, None, "public", 1)]


def test_upgrade_without_inline_columns_creates_nothing(migration):
    engine = _legacy_engine(columns=())
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO tasks (id, user_id) VALUES (1, 2)"))
    _upgrade(engine, migration)
    assert "task_acl" not in sa.inspect(engine).get_table_names()
    assert _task_columns(engine) == {"id", "user_id"}
    _upgrade(engine, migration)  # re-running stays a no-op
