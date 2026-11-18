"""Upgrading from the fork's inline tenancy: core alembic 6 stashes it, lib.tenancy maps and carries it."""

import importlib.util
import os
import pathlib
import subprocess
import sys
import types

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "utils/db_migration/versions/6_drop_inline_tenancy_columns.py"


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


def _rows(engine, sql):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(sql))]


def _task_columns(engine):
    return {c["name"] for c in sa.inspect(engine).get_columns("tasks")}


def _stashed(engine):
    return _rows(engine, "SELECT task_id, tenant_id, visibility FROM tasks_legacy_tenancy ORDER BY task_id")


# --- core: alembic migration 6 -------------------------------------------------------------


def test_upgrade_stashes_inline_tenancy_verbatim_before_dropping(migration):
    engine = _legacy_engine()
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO tasks VALUES (1, 7, 3, 'public'), (2, 7, 3, 'tenant'), (3, 7, 3, 'private'), "
            "(4, 8, NULL, 'private'), (5, 0, NULL, 'public'), (6, 8, NULL, NULL), (7, 8, 9, 'weird')"))
    _upgrade(engine, migration)
    assert {"tenant_id", "visibility"}.isdisjoint(_task_columns(engine))
    # Core copies, it does not interpret: legacy tenant ids are a plugin's business.
    assert _stashed(engine) == [(1, 3, "public"), (2, 3, "tenant"), (3, 3, "private"), (5, None, "public"), (7, 9, "weird")]
    assert "task_acl" not in sa.inspect(engine).get_table_names()


def test_upgrade_without_inline_columns_creates_no_side_table(migration):
    engine = _legacy_engine(columns=())
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO tasks VALUES (1, 7)"))
    _upgrade(engine, migration)
    assert "tasks_legacy_tenancy" not in sa.inspect(engine).get_table_names()


def test_upgrade_with_only_visibility_stashes_non_private(migration):
    engine = _legacy_engine(columns=("visibility",))
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO tasks VALUES (1, 7, 'public'), (2, 7, 'private')"))
    _upgrade(engine, migration)
    assert _stashed(engine) == [(1, None, "public")]
    assert "visibility" not in _task_columns(engine)


def test_upgrade_rerun_keeps_existing_stash(migration):
    engine = _legacy_engine()
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE tasks_legacy_tenancy (task_id INTEGER PRIMARY KEY, tenant_id INTEGER, visibility VARCHAR(16))"))
        conn.execute(sa.text("INSERT INTO tasks_legacy_tenancy VALUES (1, 5, 'tenant')"))
        conn.execute(sa.text("INSERT INTO tasks VALUES (1, 7, 3, 'public'), (2, 7, 3, 'tenant')"))
    _upgrade(engine, migration)
    assert _stashed(engine) == [(1, 5, "tenant"), (2, 3, "tenant")]


# --- plugin: reconcile consumes the stash ----------------------------------------------------


def _stash_engine(path=None):
    engine = sa.create_engine(f"sqlite:///{path}" if path else "sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER, attributes TEXT)"))
        conn.execute(sa.text("CREATE TABLE tasks_legacy_tenancy (task_id INTEGER PRIMARY KEY, tenant_id INTEGER, visibility VARCHAR(16))"))
        conn.execute(sa.text("INSERT INTO tasks (id, user_id) VALUES (1, 2), (2, 2), (3, 2), (4, 0), (5, 4), (6, 4), (7, 4)"))
        conn.execute(sa.text(
            "INSERT INTO tasks_legacy_tenancy VALUES (1, 1, 'public'), (2, 1, 'tenant'), (3, 1, 'private'), "
            "(4, NULL, 'public'), (5, 2, 'tenant'), (6, 9, 'tenant'), (99, 1, 'tenant')"))
    return engine


def _acl(engine):
    return _rows(engine, "SELECT task_id, user_id, tenant_id, visibility, visibility_seq FROM task_acl ORDER BY task_id")


def test_cleanup_carries_stash_remapped_and_fail_closed(monkeypatch):
    import lib.tenancy.backfill as backfill
    from lib.tenancy.acl import TaskAcl, ensure_acl_schema

    engine = _stash_engine()
    ensure_acl_schema(engine)
    with Session(engine) as s:
        s.add(TaskAcl(task_id=7, user_id=4, tenant_id=None, visibility="private", visibility_seq=3))
        s.commit()
    monkeypatch.setattr(backfill, "_is_multitenancy_enabled", lambda: True)
    # Legacy tenant 1 became group/tenant 11, legacy 2 was inactive, legacy 9 is unknown.
    stats = backfill.cleanup_legacy_inline_schema(engine, tenant_map={1: 11, 2: None})
    assert stats["legacy_tasks_carried"] == 6
    assert _acl(engine) == [
        (1, 2, 11, "public", 1),
        (2, 2, 11, "tenant", 1),
        (3, 2, None, "private", 1),  # owner-only, as before: no tenant for tenant admins to reach
        (4, None, None, "public", 1),  # user_id 0 is "no owner"
        (5, 4, None, "private", 1),  # inactive tenant: its shared task becomes owner-only
        (6, 4, None, "private", 1),  # unknown tenant: same
        (7, 4, None, "private", 3),  # an existing task_acl row wins
    ]
    assert "tasks_legacy_tenancy" not in sa.inspect(engine).get_table_names()


def test_cleanup_dry_run_counts_and_writes_nothing(monkeypatch):
    import lib.tenancy.backfill as backfill

    engine = _stash_engine()
    monkeypatch.setattr(backfill, "_is_multitenancy_enabled", lambda: False)
    stats = backfill.cleanup_legacy_inline_schema(engine, dry_run=True, tenant_map={1: 11})
    assert stats["legacy_tasks_carried"] == 6  # task 99 has no tasks row
    tables = set(sa.inspect(engine).get_table_names())
    assert "task_acl" not in tables and "tasks_legacy_tenancy" in tables


def test_cleanup_keeps_is_tenant_admin_until_web_import_ran(monkeypatch):
    import lib.tenancy.backfill as backfill

    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE users_tenant (id INTEGER PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE users_userprofile (id INTEGER PRIMARY KEY, is_tenant_admin BOOL)"))
        conn.execute(sa.text("CREATE TABLE django_migrations (id INTEGER PRIMARY KEY, app VARCHAR, name VARCHAR)"))
    monkeypatch.setattr(backfill, "_is_multitenancy_enabled", lambda: True)
    assert backfill.cleanup_legacy_inline_schema(engine)["dropped_is_tenant_admin"] == 0
    assert "is_tenant_admin" in {c["name"] for c in sa.inspect(engine).get_columns("users_userprofile")}
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO django_migrations (app, name) VALUES ('cape_tenancy', '0002_import_legacy_users_tenant')"))
    assert backfill.cleanup_legacy_inline_schema(engine)["dropped_is_tenant_admin"] == 1
    assert "is_tenant_admin" not in {c["name"] for c in sa.inspect(engine).get_columns("users_userprofile")}


def _reconcile_env(monkeypatch, tmp_path, docs):
    import lib.cuckoo.common.config as config_mod
    import lib.cuckoo.core.database as database_mod
    from lib.tenancy import acl as acl_mod

    path = tmp_path / "legacy.db"
    engine = _stash_engine(path)
    monkeypatch.setattr(database_mod, "init_database", lambda: None)
    monkeypatch.setattr(database_mod, "Database", lambda: types.SimpleNamespace(engine=engine))
    monkeypatch.setattr(acl_mod, "_acl_schema_ready", set())
    monkeypatch.setattr(config_mod, "Config", lambda name=None: types.SimpleNamespace(
        database=types.SimpleNamespace(connection=f"sqlite:///{path}", psql_ssl_mode="disable"),
        mongodb=types.SimpleNamespace(enabled=True), elasticsearchdb=types.SimpleNamespace(enabled=False)))
    writes = []
    collection = types.SimpleNamespace(find=lambda *a: iter(docs), bulk_write=lambda ops, **kw: writes.extend(ops))
    monkeypatch.setitem(sys.modules, "dev_utils.mongodb", types.SimpleNamespace(results_db=types.SimpleNamespace(analysis=collection)))
    return engine, writes


def test_reconcile_refuses_stash_without_web_tenant_map(monkeypatch, tmp_path, capsys):
    import lib.tenancy.backfill as backfill

    engine, writes = _reconcile_env(monkeypatch, tmp_path, [])
    monkeypatch.setattr(backfill, "load_legacy_tenant_map", lambda: None)
    try:
        assert backfill.main(["--force"]) == 2
        assert "manage.py migrate" in capsys.readouterr().err
        assert "tasks_legacy_tenancy" in sa.inspect(engine).get_table_names()
        assert writes == []
        # The operator may choose to carry them tenantless instead.
        assert backfill.main(["--force", "--drop-legacy-tenants"]) == 0
        assert {r[0]: r[2:4] for r in _acl(engine)}[2] == (None, "private")
    finally:
        engine.dispose()


def test_reconcile_maps_stash_and_restamps_legacy_public_without_adopt_flag(monkeypatch, tmp_path, capsys):
    import lib.tenancy.backfill as backfill

    # Task 4's report claims public with no tenant; the stash records it, so it is not "unrecorded".
    docs = [
        {"_id": "a", "info": {"id": 4, "visibility": "public", "tenant_id": None}},
        {"_id": "b", "info": {"id": 2, "visibility": "tenant", "tenant_id": 1}},
    ]
    engine, writes = _reconcile_env(monkeypatch, tmp_path, docs)
    monkeypatch.setattr(backfill, "load_legacy_tenant_map", lambda: {1: 11})
    monkeypatch.setattr(backfill, "_is_multitenancy_enabled", lambda: True)
    try:
        assert backfill.main(["--force"]) == 0, capsys.readouterr().err
        stamps = {w._filter["_id"]: w._doc["$set"] for w in writes}
        assert stamps["b"]["info.tenant_id"] == 11 and stamps["b"]["info.visibility"] == "tenant"
        assert "a" not in stamps or stamps["a"]["info.visibility"] == "public"
        assert "tasks_legacy_tenancy" not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()


# --- plugin: Django migration 0002 -----------------------------------------------------------


def test_django_import_maps_legacy_tenants_to_fresh_groups():
    pytest.importorskip("django")
    script = r"""
import django
from django.conf import settings
settings.configure(
    INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes", "lib.tenancy.apps.CapeTenancyConfig"],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
)
django.setup()
from django.core.management import call_command
from django.db import connection
call_command("migrate", "auth", verbosity=0)
from django.contrib.auth.models import Group, User
# An existing Group 1 would have merged with legacy tenant 1 had the ids been kept.
staff = Group.objects.create(id=1, name="staff")
alice, carol, bob, dave, erin = (User.objects.create_user(username=n) for n in ("alice", "carol", "bob", "dave", "erin"))
staff.user_set.add(dave)
with connection.cursor() as c:
    c.execute("CREATE TABLE users_tenant (id INTEGER PRIMARY KEY, slug VARCHAR(48), name VARCHAR(128), idp_groups TEXT, admin_idp_groups TEXT, active BOOL, created_at DATETIME)")
    c.execute("INSERT INTO users_tenant VALUES (1, 'acme', 'Acme', '[\"sso-acme\"]', '[]', 1, '2026-01-01'), (2, 'globex', 'Globex', '[]', '[]', 1, '2026-01-01'), (3, 'gone', 'Gone', '[]', '[]', 0, '2026-01-01')")
    c.execute("CREATE TABLE users_userprofile (id INTEGER PRIMARY KEY, user_id INTEGER, tenant_id INTEGER, is_tenant_admin BOOL)")
    c.execute("INSERT INTO users_userprofile VALUES (1, %s, 1, 0), (2, %s, 1, 1), (3, %s, 2, 0), (4, %s, NULL, 0), (5, %s, 3, 1), (6, 999, 1, 0)" % (alice.pk, carol.pk, bob.pk, dave.pk, erin.pk))
call_command("migrate", "cape_tenancy", verbosity=0)

from lib.tenancy.models import GroupSandboxPolicy, LegacyTenantMap, Tenant
from lib.tenancy.middleware import _resolve_tenant_from_user
from lib.tenancy.resources import resolve_user_entitlements, reconcile_idp_groups_to_django_groups

m = {r.legacy_tenant_id: r.tenant_id for r in LegacyTenantMap.objects.all()}
assert set(m) == {1, 2, 3} and m[3] is None, m
assert 1 not in m.values() and m[1] != m[2], m
acme = Group.objects.get(name="tenant-acme")
assert m[1] == acme.id and Tenant.objects.get(pk=acme.id).slug == "acme"
assert not Group.objects.filter(name="tenant-gone").exists()

def res(u):
    return _resolve_tenant_from_user(User.objects.get(pk=u.pk))

assert res(alice)[:2] == (m[1], False), res(alice)
assert res(carol)[:2] == (m[1], True), res(carol)
assert res(bob)[:2] == (m[2], False), res(bob)
assert res(dave)[0] == 1  # untouched: still only in staff
assert res(erin)[0] is None  # inactive tenant: nothing restored
assert resolve_user_entitlements(User.objects.get(pk=alice.pk)).can_use_machine("anything") is True
assert GroupSandboxPolicy.objects.get(group=acme).idp_groups == ["sso-acme"]

# SSO revocation still works: a login without the IdP group drops the member group.
a = User.objects.get(pk=alice.pk)
reconcile_idp_groups_to_django_groups(a, ["other"])
assert res(alice)[0] is None, res(alice)

# Re-running finds cape_tenancy populated and does nothing more.
n_groups = Group.objects.count()
mig = __import__("importlib").import_module("lib.tenancy.migrations.0002_import_legacy_users_tenant")
from django.apps import apps as global_apps
mig.import_legacy_tenancy(global_apps, type("SchemaEditor", (), {"connection": connection})())
assert Group.objects.count() == n_groups
print("OK")
"""
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=ROOT)
    assert res.returncode == 0 and "OK" in res.stdout, f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}"


def test_django_import_skips_install_already_using_cape_tenancy():
    pytest.importorskip("django")
    script = r"""
import django
from django.conf import settings
settings.configure(
    INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes", "lib.tenancy.apps.CapeTenancyConfig"],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
)
django.setup()
from django.core.management import call_command
from django.db import connection
call_command("migrate", "cape_tenancy", "0001", verbosity=0)
from django.contrib.auth.models import Group
from lib.tenancy.models import GroupSandboxPolicy
GroupSandboxPolicy.objects.create(group=Group.objects.create(name="ops"))
with connection.cursor() as c:
    c.execute("CREATE TABLE users_tenant (id INTEGER PRIMARY KEY, slug VARCHAR(48), name VARCHAR(128), idp_groups TEXT, admin_idp_groups TEXT, active BOOL, created_at DATETIME)")
    c.execute("INSERT INTO users_tenant VALUES (1, 'acme', 'Acme', '[]', '[]', 1, '2026-01-01')")
call_command("migrate", "cape_tenancy", verbosity=0)
assert list(Group.objects.values_list("name", flat=True)) == ["ops"]
print("OK")
"""
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=ROOT)
    assert res.returncode == 0 and "OK" in res.stdout, f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}"
