"""Batched MongoDB tenancy backfill utility."""

from __future__ import annotations

import sys
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import String, cast, select
from sqlalchemy.orm import Session

from lib.tenancy.acl import TaskAcl
from lib.tenancy.policy import PRIVATE, PUBLIC, TENANT, VISIBILITIES

# Written by core alembic migration 6 when it drops the inline tasks.tenant_id / visibility columns.
LEGACY_TENANCY_TABLE = "tasks_legacy_tenancy"
LEGACY_IMPORT_MIGRATION = ("cape_tenancy", "0002_import_legacy_users_tenant")


def backfill_legacy_uniform(
    mongo_update_many: Callable[[str, Dict[str, Any], Dict[str, Any]], Any],
    *,
    default_visibility: str = PUBLIC,
    collection: str = "analysis",
) -> Any:
    """Single-query fast path: stamp all legacy analysis docs lacking `info.visibility`."""
    if default_visibility not in VISIBILITIES:
        raise ValueError(f"Invalid default_visibility: {default_visibility!r}")

    query = {
        "$or": [
            {"info.visibility": {"$exists": False}},
            {"info.visibility": None},
        ]
    }
    update = {
        "$set": {
            "info.visibility": default_visibility,
            "info.tenant_id": None,
            "info.visibility_seq": 1,
        }
    }
    return mongo_update_many(collection, query, update)


def _chunked(items: Sequence[int], chunk_size: int) -> Iterable[Sequence[int]]:
    for idx in range(0, len(items), chunk_size):
        yield items[idx : idx + chunk_size]


def backfill_from_task_acl_batched(
    session: Session,
    task_ids: Sequence[int],
    mongo_bulk_updater: Callable[[List[Tuple[Dict[str, Any], Dict[str, Any]]]], int],
    *,
    chunk_size: int = 500,
    fallback_visibility: str = PRIVATE,
) -> int:
    """Batch-resolve `TaskAcl` in chunks of `chunk_size` and invoke `mongo_bulk_updater` once per chunk."""
    if fallback_visibility not in VISIBILITIES:
        raise ValueError(f"Invalid fallback_visibility: {fallback_visibility!r}")

    total_updated = 0
    for batch_ids in _chunked([int(x) for x in task_ids], max(1, chunk_size)):
        stmt = select(TaskAcl).where(TaskAcl.task_id.in_(batch_ids))
        rows_by_id: Dict[int, TaskAcl] = {row.task_id: row for row in session.scalars(stmt).all()}

        ops: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for tid in batch_ids:
            acl = rows_by_id.get(tid)
            if acl is not None:
                stamp = {
                    "info.user_id": acl.user_id,
                    "info.tenant_id": acl.tenant_id,
                    "info.visibility": acl.visibility,
                    "info.visibility_seq": acl.visibility_seq,
                }
            else:
                stamp = {
                    "info.user_id": None,
                    "info.tenant_id": None,
                    "info.visibility": fallback_visibility,
                    "info.visibility_seq": 1,
                }
            flt = {
                "info.id": tid,
                "$or": [
                    {"info.visibility": {"$exists": False}},
                    {"info.visibility": None},
                ],
            }
            ops.append((flt, {"$set": stamp}))

        if ops:
            total_updated += int(mongo_bulk_updater(ops) or 0)

    return total_updated


_STAMP_FIELDS = ("user_id", "tenant_id", "visibility", "visibility_seq")


def _acl_stamp(acl: TaskAcl) -> Dict[str, Any]:
    return {
        "user_id": acl.user_id,
        "tenant_id": acl.tenant_id,
        "visibility": acl.visibility,
        "visibility_seq": int(acl.visibility_seq or 1),
    }


def plan_reconcile_stamps(
    session: Session,
    docs: Iterable[Dict[str, Any]],
    *,
    chunk_size: int = 500,
    fallback_visibility: str = PRIVATE,
    preserve_public_without_acl: bool = False,
    adopt_public_without_acl: bool = False,
    abort_on_unrecorded_public: bool = False,
    unrecorded_public: Optional[List[int]] = None,
    task_owners: Optional[Callable[[Iterable[int]], Dict[int, Any]]] = None,
    skipped: Optional[List[Any]] = None,
) -> Iterable[List[Tuple[Any, Dict[str, Any]]]]:
    """Yield batches of `(doc key, stamp)` for report docs whose stamp differs from `task_acl`.

    Each doc is `{"key", "id", <stamp fields>}`. Unlike the backfills above, this
    overwrites existing stamps: pre-stack installs stamped `info.tenant_id` from their own Tenant
    ids, which collide with the Group ids used now.

    A doc is matched to the ACL row by `info.id`. Any doc without a valid `info.id` is left alone
    and its key appended to `skipped`. A matched doc without an ACL row is
    stamped `fallback_visibility` with no tenant, owned (as `resolve_task_scope` resolves it) by
    its task's `tasks.user_id` (unless `preserve_public_without_acl` is True and the doc already
    carries `visibility='public'` with `tenant_id=None`).
    """
    if fallback_visibility not in VISIBILITIES:
        raise ValueError(f"Invalid fallback_visibility: {fallback_visibility!r}")
    fallback = {"user_id": None, "tenant_id": None, "visibility": fallback_visibility, "visibility_seq": 1}
    owners_of = task_owners or (lambda ids: _task_owners(session, ids))

    def _flush(batch: List[Dict[str, Any]]) -> List[Tuple[Any, Dict[str, Any]]]:
        task_ids = {int(d["id"]) for d in batch if isinstance(d.get("id"), int)}
        by_task: Dict[int, TaskAcl] = {}
        if task_ids:
            by_task = {r.task_id: r for r in session.scalars(select(TaskAcl).where(TaskAcl.task_id.in_(task_ids)))}
        unmatched = {i for i in task_ids if i not in by_task}
        owners = owners_of(unmatched) if unmatched else {}
        ops = []
        for doc in batch:
            tid = doc.get("id") if isinstance(doc.get("id"), int) else None
            if tid is None:
                if skipped is not None:
                    skipped.append(doc["key"])
                continue
            acl = by_task.get(tid)
            if acl is not None:
                stamp = _acl_stamp(acl)
            else:
                stamp = dict(fallback)
                stamp["user_id"] = owners.get(tid) or None  # tasks.user_id 0 is add()'s "no owner"
                if doc.get("visibility") == PUBLIC and doc.get("tenant_id") is None:
                    if unrecorded_public is not None and tid not in unrecorded_public:
                        unrecorded_public.append(tid)
                    if abort_on_unrecorded_public:
                        continue
                    if preserve_public_without_acl or adopt_public_without_acl:
                        stamp["visibility"] = PUBLIC
                        if adopt_public_without_acl and tid not in by_task:
                            new_row = TaskAcl(
                                task_id=int(tid),
                                user_id=stamp["user_id"],
                                tenant_id=None,
                                visibility=PUBLIC,
                                visibility_seq=1,
                            )
                            session.add(new_row)
                            by_task[int(tid)] = new_row
            if any(doc.get(field) != stamp[field] for field in _STAMP_FIELDS):
                ops.append((doc["key"], stamp))
        return ops

    batch: List[Dict[str, Any]] = []
    for doc in docs:
        batch.append(doc)
        if len(batch) >= max(1, chunk_size):
            ops = _flush(batch)
            batch = []
            if ops:
                yield ops
    if batch:
        ops = _flush(batch)
        if ops:
            yield ops


def _task_owners(session: Session, task_ids: Iterable[int]) -> Dict[int, Any]:
    from lib.cuckoo.core.data.task import Task

    return dict(session.execute(select(Task.id, Task.user_id).where(Task.id.in_(list(task_ids)))).all())


def _doc_from_info(key: Any, info: Any) -> Dict[str, Any]:
    info = info if isinstance(info, dict) else {}
    doc = {"key": key, "id": info.get("id")}
    for field in _STAMP_FIELDS:
        doc[field] = info.get(field)
    return doc


_INFO_PROJECTION = ["info.id"] + [f"info.{field}" for field in _STAMP_FIELDS]


def _mongo_reconcile_filter(key: Any, stamp: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "_id": key,
        "$or": [
            {"info.visibility_seq": {"$lte": int(stamp["visibility_seq"])}},
            {"info.visibility_seq": {"$exists": False}},
            {"info.visibility_seq": None},
        ],
    }


def _es_reconcile_action(index: str, doc_id: str, stamp: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "_op_type": "update",
        "_index": index,
        "_id": doc_id,
        "script": {
            "lang": "painless",
            "source": (
                "def s = ctx._source.info != null ? ctx._source.info.visibility_seq : null;"
                " if (s != null && s > params.stamp.visibility_seq) { ctx.op = 'noop'; }"
                " else { if (ctx._source.info == null) { ctx._source.info = [:]; }"
                " for (e in params.stamp.entrySet()) { ctx._source.info[e.getKey()] = e.getValue(); } }"
            ),
            "params": {"stamp": stamp},
        },
    }


def reconcile_mongo_stamps(
    session: Session,
    *,
    find: Callable[[str, Dict[str, Any], Dict[str, Any]], Iterable[Dict[str, Any]]],
    bulk_update: Callable[[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]], Any],
    collection: str = "analysis",
    chunk_size: int = 500,
    dry_run: bool = False,
    preserve_public_without_acl: bool = False,
    adopt_public_without_acl: bool = False,
    abort_on_unrecorded_public: bool = False,
    unrecorded_public: Optional[List[int]] = None,
    task_owners: Optional[Callable[[Iterable[int]], Dict[int, Any]]] = None,
    skipped: Optional[List[Any]] = None,
) -> int:
    """Restamp every Mongo report doc from `task_acl` (see `plan_reconcile_stamps`); returns docs changed."""
    raw = find(collection, {}, {field: 1 for field in _INFO_PROJECTION})
    docs = (_doc_from_info(d["_id"], d.get("info")) for d in raw)
    changed = 0
    for ops in plan_reconcile_stamps(
        session,
        docs,
        chunk_size=chunk_size,
        preserve_public_without_acl=preserve_public_without_acl,
        adopt_public_without_acl=adopt_public_without_acl and not dry_run,
        abort_on_unrecorded_public=abort_on_unrecorded_public,
        unrecorded_public=unrecorded_public,
        task_owners=task_owners,
        skipped=skipped,
    ):
        changed += len(ops)
        if not dry_run:
            bulk_update(
                collection,
                [
                    (_mongo_reconcile_filter(key, stamp), {"$set": {f"info.{k}": v for k, v in stamp.items()}})
                    for key, stamp in ops
                ],
            )
    if adopt_public_without_acl and not dry_run:
        session.commit()
    return changed


def reconcile_es_stamps(
    session: Session,
    *,
    scan: Callable[[], Iterable[Dict[str, Any]]],
    update: Optional[Callable[[str, str, Dict[str, Any]], Any]] = None,
    bulk_update: Optional[Callable[[List[Tuple[Tuple[str, str], Dict[str, Any]]]], Any]] = None,
    chunk_size: int = 500,
    dry_run: bool = False,
    preserve_public_without_acl: bool = False,
    adopt_public_without_acl: bool = False,
    abort_on_unrecorded_public: bool = False,
    unrecorded_public: Optional[List[int]] = None,
    task_owners: Optional[Callable[[Iterable[int]], Dict[int, Any]]] = None,
    skipped: Optional[List[Any]] = None,
) -> int:
    """Restamp every Elasticsearch analysis doc from `task_acl`; `scan` yields hits with `_source.info`."""
    docs = (_doc_from_info((h["_index"], h["_id"]), (h.get("_source") or {}).get("info")) for h in scan())
    changed = 0
    for ops in plan_reconcile_stamps(
        session,
        docs,
        chunk_size=chunk_size,
        preserve_public_without_acl=preserve_public_without_acl,
        adopt_public_without_acl=adopt_public_without_acl and not dry_run,
        abort_on_unrecorded_public=abort_on_unrecorded_public,
        unrecorded_public=unrecorded_public,
        task_owners=task_owners,
        skipped=skipped,
    ):
        if dry_run:
            changed += len(ops)
            continue
        if bulk_update is not None:
            res = bulk_update(ops)
            if isinstance(res, tuple) and len(res) == 2:
                ok_count, errors = res
                if errors:
                    raise RuntimeError(f"Elasticsearch bulk restamp failed for {len(errors)} item(s): {errors[:3]!r}")
                changed += int(ok_count) if isinstance(ok_count, int) else len(ops)
            elif isinstance(res, int):
                changed += res
            else:
                changed += len(ops)
        elif update is not None:
            for (index, doc_id), stamp in ops:
                update(index, doc_id, stamp)
            changed += len(ops)
        else:
            changed += len(ops)
    if adopt_public_without_acl and not dry_run:
        session.commit()
    return changed


def _resync_one(session: Session, task_id: int) -> bool:
    """Re-apply task_id's task_acl to its Mongo/ES report (whichever reporting enables); False for an
    unknown task. A task without a row is stamped private to its submitter, as the full reconcile does."""
    from lib.cuckoo.common.config import Config
    from lib.tenancy.acl import resync_report_stamp
    from lib.tenancy.views import _default_es_update_by_query, _default_mongo_update_one

    owners = _task_owners(session, [int(task_id)])
    if int(task_id) not in owners:
        return False
    reporting = Config("reporting")
    stamp = resync_report_stamp(
        session,
        task_id,
        owner_if_no_row=owners[int(task_id)],
        mongo_update_one=_default_mongo_update_one if reporting.mongodb.enabled else None,
        es_update_by_query=_default_es_update_by_query() if reporting.elasticsearchdb.enabled else None,
    )
    return stamp is not None


def runs_dist_tasks(session: Session) -> Optional[int]:
    """The id of a task this node runs for another server, or None: a dist `main_task_id`.
    Their reports carry that server's ids."""
    from lib.cuckoo.core.data.task import Task

    attr_stmt = select(Task.id, Task.attributes).where(
        Task.attributes.is_not(None),
        cast(Task.attributes, String).like('%"main_task_id"%'),
    )
    for tid, attributes in session.execute(attr_stmt).yield_per(1000):
        if isinstance(attributes, dict) and attributes.get("main_task_id"):
            return tid
    return None


def _is_multitenancy_enabled() -> bool:
    try:
        from lib.cuckoo.common.config import Config

        return bool(getattr(Config(), "multitenancy", {}).get("enabled", False))
    except Exception:
        return False


def load_legacy_tenant_map() -> Optional[Dict[int, Optional[int]]]:
    """Return `{legacy tenant id: tenant id or None}` from cape_tenancy, or None when unavailable.

    None means the web side has not imported the legacy tenants yet (cape_tenancy not installed,
    its migration 0002 not applied, or Django not configurable from here).
    """
    import os

    from lib.cuckoo.common.constants import CUCKOO_ROOT

    web = os.path.join(CUCKOO_ROOT, "web")
    cwd = os.getcwd()
    try:
        import django
        from django.apps import apps

        # web/web/settings.py names its SQLite database relative to web/ (as manage.py runs there).
        os.chdir(web)
        if not apps.ready:
            if web not in sys.path:
                sys.path.insert(0, web)
            os.environ.setdefault("DJANGO_SETTINGS_MODULE", "web.settings")
            django.setup()
        if not apps.is_installed("lib.tenancy"):
            return None
        from django.db import connection
        from django.db.migrations.recorder import MigrationRecorder

        if LEGACY_IMPORT_MIGRATION not in MigrationRecorder(connection).applied_migrations():
            return None
        model = apps.get_model("cape_tenancy", "LegacyTenantMap")
        return {int(m.legacy_tenant_id): m.tenant_id for m in model.objects.all()}
    except Exception as error:  # noqa: BLE001 - any failure means "not available", and the caller refuses
        print(f"could not read the legacy tenant map from the web database: {error}", file=sys.stderr)
        return None
    finally:
        os.chdir(cwd)


def staged_legacy_tenancy(engine) -> Optional[Dict[str, Any]]:
    """Summarize LEGACY_TENANCY_TABLE: None if absent, else whether it holds tenant ids and its public task ids."""
    import sqlalchemy as sa

    with engine.connect() as conn:
        if LEGACY_TENANCY_TABLE not in set(sa.inspect(conn).get_table_names()):
            return None
        has_tenants = (
            conn.execute(sa.text(f"SELECT 1 FROM {LEGACY_TENANCY_TABLE} WHERE tenant_id IS NOT NULL LIMIT 1")).first()
            is not None
        )
        public = {
            int(r[0])
            for r in conn.execute(sa.text(f"SELECT task_id FROM {LEGACY_TENANCY_TABLE} WHERE visibility = 'public'"))
        }
    return {"has_tenants": has_tenants, "public_task_ids": public}


def _legacy_acl_rows(conn, tables, tenant_map: Dict[int, Optional[int]], carry_public: bool) -> List[Dict[str, Any]]:
    """task_acl rows for the stashed legacy tasks: legacy tenant ids remapped, fail-closed otherwise."""
    import sqlalchemy as sa

    sql = (
        f"SELECT l.task_id, l.tenant_id, l.visibility, t.user_id FROM {LEGACY_TENANCY_TABLE} l "
        "JOIN tasks t ON t.id = l.task_id"
    )
    if "task_acl" in tables:
        sql += " WHERE NOT EXISTS (SELECT 1 FROM task_acl a WHERE a.task_id = l.task_id)"
    rows = []
    for task_id, legacy_tenant, visibility, user_id in conn.execute(sa.text(sql)):
        vis = visibility if visibility in (PUBLIC, TENANT) else PRIVATE
        if vis == PUBLIC and not carry_public:
            continue
        tenant = tenant_map.get(int(legacy_tenant)) if legacy_tenant is not None else None
        if vis == TENANT and tenant is None:
            vis = PRIVATE  # its tenant is gone (inactive or unknown): owner-only
        if vis == PRIVATE:
            tenant = None  # legacy private was owner-only; a tenant here would open it to tenant admins
        rows.append(
            {"task_id": int(task_id), "user_id": user_id or None, "tenant_id": tenant, "visibility": vis, "visibility_seq": 1}
        )
    return rows


def _legacy_import_applied(conn, tables) -> bool:
    import sqlalchemy as sa

    if "django_migrations" not in tables:
        return False
    app, name = LEGACY_IMPORT_MIGRATION
    return (
        conn.execute(sa.text("SELECT 1 FROM django_migrations WHERE app = :a AND name = :n"), {"a": app, "n": name}).first()
        is not None
    )


def cleanup_legacy_inline_schema(
    engine, *, dry_run: bool = False, tenant_map: Optional[Dict[int, Optional[int]]] = None
) -> Dict[str, int]:
    """Clean up legacy inline-tenancy SQL state.

    Carries `tasks.visibility='public'` (inline columns still present) or the rows alembic
    migration 6 stashed in LEGACY_TENANCY_TABLE into `task_acl`, remapping legacy tenant ids
    through `tenant_map` (see `load_legacy_tenant_map`), then drops the stash and
    `users_userprofile.is_tenant_admin`.
    """
    import sqlalchemy as sa

    from lib.tenancy.acl import ensure_acl_schema

    stats = {"public_tasks_carried": 0, "legacy_tasks_carried": 0, "dropped_is_tenant_admin": 0}
    with engine.begin() as conn:
        insp = sa.inspect(conn)
        tables = set(insp.get_table_names())
        if LEGACY_TENANCY_TABLE in tables:
            has_tenant_rows = (
                conn.execute(sa.text(f"SELECT 1 FROM {LEGACY_TENANCY_TABLE} WHERE tenant_id IS NOT NULL LIMIT 1")).first()
                is not None
            )
            rows = _legacy_acl_rows(conn, tables, tenant_map or {}, _is_multitenancy_enabled() or has_tenant_rows)
            stats["legacy_tasks_carried"] = len(rows)
            if not dry_run:
                if rows:
                    if "task_acl" not in tables:
                        ensure_acl_schema(conn)
                        tables.add("task_acl")
                    conn.execute(
                        sa.text(
                            "INSERT INTO task_acl (task_id, user_id, tenant_id, visibility, visibility_seq) "
                            "VALUES (:task_id, :user_id, :tenant_id, :visibility, :visibility_seq)"
                        ),
                        rows,
                    )
                conn.execute(sa.text(f"DROP TABLE {LEGACY_TENANCY_TABLE}"))
        if "tasks" in tables:
            columns = {c["name"] for c in insp.get_columns("tasks")}
            if "visibility" in columns:
                has_tenant_rows = (
                    conn.execute(sa.text("SELECT 1 FROM tasks WHERE tenant_id IS NOT NULL LIMIT 1")).first() is not None
                    if "tenant_id" in columns
                    else False
                )
                if _is_multitenancy_enabled() or has_tenant_rows:
                    if dry_run:
                        if "task_acl" in tables:
                            cnt = conn.execute(
                                sa.text(
                                    "SELECT COUNT(*) FROM tasks t WHERE t.visibility = 'public' "
                                    "AND NOT EXISTS (SELECT 1 FROM task_acl a WHERE a.task_id = t.id)"
                                )
                            ).scalar()
                        else:
                            cnt = conn.execute(sa.text("SELECT COUNT(*) FROM tasks WHERE visibility = 'public'")).scalar()
                        stats["public_tasks_carried"] = int(cnt or 0)
                    else:
                        has_public = conn.execute(sa.text("SELECT 1 FROM tasks WHERE visibility = 'public' LIMIT 1")).first()
                        if has_public is not None:
                            if "task_acl" not in tables:
                                ensure_acl_schema(conn)
                            res = conn.execute(
                                sa.text(
                                    "INSERT INTO task_acl (task_id, user_id, tenant_id, visibility, visibility_seq) "
                                    "SELECT t.id, t.user_id, NULL, 'public', 1 FROM tasks t "
                                    "WHERE t.visibility = 'public' "
                                    "AND NOT EXISTS (SELECT 1 FROM task_acl a WHERE a.task_id = t.id)"
                                )
                            )
                            stats["public_tasks_carried"] = int(res.rowcount or 0)
        if "users_userprofile" in tables:
            up_cols = {c["name"] for c in insp.get_columns("users_userprofile")}
            # The web migration that imports legacy tenant admins reads this column: keep it until then.
            if "is_tenant_admin" in up_cols and ("users_tenant" not in tables or _legacy_import_applied(conn, tables)):
                if not dry_run:
                    conn.execute(sa.text("ALTER TABLE users_userprofile DROP COLUMN is_tenant_admin"))
                stats["dropped_is_tenant_admin"] = 1
    return stats



class DryRunUnavailable(RuntimeError):
    """The existing database cannot be inspected without initialization."""


class AclSchemaMissing(DryRunUnavailable):
    """`tasks` exists but `task_acl` does not yet: a pre-stack install that was just migrated."""


def _readonly_reconcile_engine():
    """Open an existing SQL database without the schema/version writes in Database()."""
    from pathlib import Path

    from sqlalchemy import create_engine, event, inspect
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError

    from lib.cuckoo.common.config import Config
    from lib.cuckoo.common.constants import CUCKOO_ROOT

    cfg = Config().database
    url = make_url(cfg.connection or f"sqlite:///{CUCKOO_ROOT}/db/cuckoo.db")
    engine_args = {}
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:":
            raise DryRunUnavailable("dry-run requires an existing on-disk SQLite database")
        database = url.database
        if not database.startswith("file:"):
            database = Path(database).absolute().as_uri()
        url = url.set(database=database).update_query_dict({"mode": "ro", "uri": "true"})
    elif url.get_backend_name() == "postgresql":
        if url.drivername == "postgresql":
            try:
                import psycopg  # noqa: F401
                url = url.set(drivername="postgresql+psycopg")
            except ImportError:
                pass
        if url.drivername not in ("postgresql", "postgresql+psycopg", "postgresql+psycopg2"):
            raise DryRunUnavailable("dry-run requires the psycopg or psycopg2 PostgreSQL driver")
        engine_args["connect_args"] = {"sslmode": cfg.psql_ssl_mode}
    else:
        raise DryRunUnavailable("dry-run supports read-only SQLite and PostgreSQL connections only")
    engine = create_engine(url, **engine_args)

    @event.listens_for(engine, "connect")
    def enforce_read_only(connection, record):
        cursor = connection.cursor()
        try:
            if engine.dialect.name == "sqlite":
                cursor.execute("PRAGMA query_only = ON")
            else:
                cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                connection.commit()
        finally:
            cursor.close()

    try:
        with engine.connect() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            if "tasks" not in tables:
                raise DryRunUnavailable("dry-run requires existing tasks and task_acl schema; no schema was created")
            if "task_acl" not in tables:
                raise AclSchemaMissing("dry-run requires existing tasks and task_acl schema; no schema was created")
            if "attributes" not in {c["name"] for c in inspector.get_columns("tasks")}:
                raise DryRunUnavailable("dry-run requires the migrated tasks.attributes schema; no migration was run")
    except (SQLAlchemyError, DryRunUnavailable) as error:
        engine.dispose()
        if isinstance(error, DryRunUnavailable):
            raise
        raise DryRunUnavailable("dry-run could not open the existing database read-only; no initialization was attempted") from error
    return engine


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`utils/tenancy_reconcile.py [--dry-run]`: restamp Mongo/ES report docs from `task_acl`.

    Run it once after upgrading a pre-stack install, with the web and processing services stopped
    (the restamp overwrites, so a visibility toggle racing it can be undone).
    """
    import argparse

    from lib.cuckoo.common.config import Config
    from lib.cuckoo.core.database import Database, init_database
    from lib.tenancy.acl import ensure_acl_schema_once

    parser = argparse.ArgumentParser(prog="utils/tenancy_reconcile.py")
    parser.add_argument("--dry-run", action="store_true", help="count docs that would change, write nothing")
    parser.add_argument(
        "--task", type=int, help="only re-apply this task's current task_acl to its report (e.g. after a worker reprocess)"
    )
    parser.add_argument(
        "--preserve-public",
        "--adopt-public-from-reports",
        dest="preserve_public",
        action="store_true",
        help="adopt visibility='public' into task_acl for tenantless public reports that have no task_acl row",
    )
    parser.add_argument(
        "--demote-public",
        action="store_true",
        help="demote tenantless public reports that have no task_acl row to private",
    )
    parser.add_argument(
        "--drop-legacy-tenants",
        action="store_true",
        help="carry stashed pre-plugin tasks without their tenant (tenant-shared ones become private) when the "
        "web side cannot map the legacy tenant ids (cape_tenancy not installed or not migrated)",
    )
    parser.add_argument(
        "--force", action="store_true", help="run although this node looks like a worker (only on the main/central server)"
    )
    args = parser.parse_args(argv)
    verb = "would be restamped" if args.dry_run else "restamped"
    abort_on_unrecorded_public = not (args.preserve_public or args.demote_public or args.dry_run)
    unrecorded_public: List[int] = []

    def _report(store: str, changed: int, skipped: List[Any]) -> None:
        print(f"{store}: {changed} analysis docs {verb}; {len(skipped)} left alone (a broker job's report)")

    # Refusing ambiguous public reports must happen before *any* SQL cleanup,
    # initialization or report-store write, including writes to an earlier backend.
    # Services must remain stopped for both passes: this is a preflight, not a
    # distributed transaction/snapshot across SQL, MongoDB and Elasticsearch.
    engine = None
    if abort_on_unrecorded_public and args.task is None:
        try:
            preflight_engine = _readonly_reconcile_engine()
        except AclSchemaMissing:
            # A just-upgraded pre-stack install has no task_acl yet, and the read-only preflight
            # needs one. Creating the empty table is schema-only: no task data and no report store
            # is written, and with no rows every tenantless public report is (correctly) unrecorded.
            init_database()
            engine = Database().engine
            ensure_acl_schema_once(engine)
            try:
                preflight_engine = _readonly_reconcile_engine()
            except DryRunUnavailable as error:
                print(str(error), file=sys.stderr)
                return 2
        except DryRunUnavailable as error:
            print(str(error), file=sys.stderr)
            return 2
        preflight_args = argparse.Namespace(**vars(args))
        preflight_args.dry_run = True
        try:
            result = _run_reconcile(
                preflight_args, preflight_engine, Config("reporting"), True, unrecorded_public,
                lambda *args: None,
            )
        finally:
            preflight_engine.dispose()
        if result:
            return result
        unrecorded_public.clear()  # entries the preflight forgave (stashed public tasks) must not resurface

    if args.dry_run:
        try:
            engine = _readonly_reconcile_engine()
        except DryRunUnavailable as error:
            print(str(error), file=sys.stderr)
            return 2
    else:
        if engine is None:
            init_database()
            engine = Database().engine
        ensure_acl_schema_once(engine)
    try:
        return _run_reconcile(args, engine, Config("reporting"), abort_on_unrecorded_public, unrecorded_public, _report)
    finally:
        if args.dry_run:
            engine.dispose()


def _run_reconcile(args, engine, reporting, abort_on_unrecorded_public, unrecorded_public, _report):
    with Session(engine) as session:
        foreign = None if args.force else runs_dist_tasks(session)
        if args.task is not None and foreign is None:
            if args.dry_run:
                print(f"task {args.task}: would restamp its report from task_acl")
                return 0
            if not _resync_one(session, args.task):
                print(f"task {args.task} not found", file=sys.stderr)
                return 1
            print(f"task {args.task}: report restamped from task_acl")
            return 0
        if foreign is not None:
            # A worker files those reports under the other server's task ids: its own task_acl rows
            # (worker-local ids) would restamp unrelated reports.
            print(
                f"this node runs tasks for a main/central server (e.g. task {foreign}); run utils/tenancy_reconcile.py "
                "there, or pass --force if this is that server",
                file=sys.stderr,
            )
            return 2
        staged = staged_legacy_tenancy(engine)
        tenant_map: Optional[Dict[int, Optional[int]]] = {}
        if staged is not None and staged["has_tenants"]:
            tenant_map = load_legacy_tenant_map()
            if tenant_map is None:
                if not args.drop_legacy_tenants:
                    print(
                        f"{LEGACY_TENANCY_TABLE} holds tasks of pre-plugin tenants, but the web database has no "
                        "cape_tenancy legacy tenant map: enable [multitenancy] and run `python3 manage.py migrate` "
                        "in web/ first, or pass --drop-legacy-tenants to carry those tasks without their tenant",
                        file=sys.stderr,
                    )
                    return 2
                tenant_map = {}
        cleanup_legacy_inline_schema(engine, dry_run=args.dry_run, tenant_map=tenant_map)
        # A dry run carries nothing, so the stashed public tasks still lack a task_acl row: they are
        # recorded (in the stash), not "unrecorded public reports" needing --adopt/--demote.
        pending_public = (
            staged["public_task_ids"]
            if staged is not None and args.dry_run and (_is_multitenancy_enabled() or staged["has_tenants"])
            else set()
        )
        if reporting.mongodb.enabled:
            from pymongo import UpdateOne

            from dev_utils.mongodb import results_db

            def _find(collection: str, query: Dict[str, Any], projection: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
                # The raw collection: the tenancy-scoped mongo_find would hide other tenants' docs.
                return getattr(results_db, collection).find(query, projection)

            def _bulk(collection: str, ops: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> Any:
                return getattr(results_db, collection).bulk_write([UpdateOne(f, u) for f, u in ops], ordered=False)

            skipped: List[Any] = []
            n = reconcile_mongo_stamps(
                session,
                find=_find,
                bulk_update=_bulk,
                dry_run=args.dry_run,
                preserve_public_without_acl=args.preserve_public,
                adopt_public_without_acl=args.preserve_public,
                abort_on_unrecorded_public=abort_on_unrecorded_public,
                unrecorded_public=unrecorded_public,
                skipped=skipped,
            )
            _report("mongodb", n, skipped)
        if reporting.elasticsearchdb.enabled:
            from elasticsearch.helpers import bulk, scan

            from dev_utils.elasticsearchdb import elastic_handler, get_analysis_index

            def _scan() -> Iterable[Dict[str, Any]]:
                return scan(
                    elastic_handler, index=get_analysis_index(), query={"query": {"match_all": {}}}, _source=_INFO_PROJECTION
                )

            def _es_bulk(ops: List[Tuple[Tuple[str, str], Dict[str, Any]]]) -> Any:
                actions = [_es_reconcile_action(index, doc_id, stamp) for (index, doc_id), stamp in ops]
                ok_count, errors = bulk(elastic_handler, actions, raise_on_error=False)
                if errors:
                    raise RuntimeError(f"Elasticsearch bulk restamp failed for {len(errors)} document(s): {errors[:3]!r}")
                return ok_count

            skipped = []
            n = reconcile_es_stamps(
                session,
                scan=_scan,
                bulk_update=_es_bulk,
                dry_run=args.dry_run,
                preserve_public_without_acl=args.preserve_public,
                adopt_public_without_acl=args.preserve_public,
                abort_on_unrecorded_public=abort_on_unrecorded_public,
                unrecorded_public=unrecorded_public,
                skipped=skipped,
            )
            _report("elasticsearch", n, skipped)
        remaining_public = [t for t in unrecorded_public if t not in pending_public]
        if abort_on_unrecorded_public and remaining_public:
            print(
                f"found {len(remaining_public)} public report(s) with no task_acl row (tasks: {remaining_public[:20]}); "
                "re-run with --adopt-public-from-reports (or --preserve-public) to record them in task_acl, "
                "or --demote-public to restamp them private",
                file=sys.stderr,
            )
            return 3
    return 0
