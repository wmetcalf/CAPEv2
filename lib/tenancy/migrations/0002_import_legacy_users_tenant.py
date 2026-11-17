"""Import the fork's legacy `users_tenant` / `users_userprofile` tenancy into `cape_tenancy`.

Deployments that ran the old inline tenancy kept tenants in `users_tenant` and each user's
tenant in `users_userprofile.tenant_id` / `is_tenant_admin`. Alembic migration 6 carries each
task's `tenant_id` into `task_acl` unchanged, so tenants keep their ids here for those
references to stay valid. Installs without `users_tenant` are untouched; re-running is a no-op.
"""

import json

from django.core.management.color import no_style
from django.db import migrations


def _rows(cursor, sql):
    cursor.execute(sql)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _json_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = json.loads(value)
    return list(value or [])


def import_legacy_tenancy(apps, schema_editor):
    connection = schema_editor.connection
    tables = set(connection.introspection.table_names())
    if "users_tenant" not in tables:
        return
    Tenant = apps.get_model("cape_tenancy", "Tenant")
    Membership = apps.get_model("cape_tenancy", "UserTenantMembership")
    alias = schema_editor.connection.alias

    with connection.cursor() as cursor:
        legacy_tenants = _rows(cursor, "SELECT id, slug, name, idp_groups, admin_idp_groups, active, created_at FROM users_tenant")
        profile_cols = set()
        if "users_userprofile" in tables:
            profile_cols = {c.name for c in connection.introspection.get_table_description(cursor, "users_userprofile")}
        profiles = []
        if "tenant_id" in profile_cols:
            admin = "is_tenant_admin" if "is_tenant_admin" in profile_cols else "0"
            profiles = _rows(
                cursor,
                f"SELECT user_id, tenant_id, {admin} AS is_tenant_admin FROM users_userprofile WHERE tenant_id IS NOT NULL",
            )

    created = False
    for row in legacy_tenants:
        existing = Tenant.objects.using(alias).filter(pk=row["id"]).first()
        if existing is not None:
            if existing.slug != row["slug"]:
                raise RuntimeError(
                    f"legacy tenant {row['id']} ({row['slug']!r}) collides with cape_tenancy tenant "
                    f"{existing.pk} ({existing.slug!r}); task_acl tenant ids would point at the wrong tenant"
                )
            continue
        clash = Tenant.objects.using(alias).filter(slug=row["slug"]).first()
        if clash is not None:
            raise RuntimeError(
                f"legacy tenant {row['slug']!r} (id {row['id']}) already exists in cape_tenancy as id {clash.pk}; "
                "task_acl tenant ids would point at the wrong tenant"
            )
        tenant = Tenant(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            idp_groups=_json_list(row["idp_groups"]),
            admin_idp_groups=_json_list(row["admin_idp_groups"]),
            active=bool(row["active"]),
        )
        tenant.save(using=alias, force_insert=True)
        # auto_now_add overwrote it on insert; keep the original creation time.
        if row["created_at"] is not None:
            Tenant.objects.using(alias).filter(pk=tenant.pk).update(created_at=row["created_at"])
        created = True

    if created:
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), [Tenant]):
                cursor.execute(sql)

    for row in profiles:
        if Membership.objects.using(alias).filter(user_id=row["user_id"]).exists():
            continue
        Membership.objects.using(alias).create(
            user_id=row["user_id"], tenant_id=row["tenant_id"], is_tenant_admin=bool(row["is_tenant_admin"])
        )


class Migration(migrations.Migration):
    dependencies = [
        ("cape_tenancy", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(import_legacy_tenancy, migrations.RunPython.noop),
    ]
