"""Turn the fork's legacy `users_tenant` / `users_userprofile` tenancy into Django Groups.

Deployments that ran the old inline tenancy kept tenants in `users_tenant` and each user's
tenant in `users_userprofile.tenant_id` / `is_tenant_admin`. Their ids cannot be reused here:
tenant ids now share one id space with Django Group ids, so a legacy tenant 1 would merge with
whatever Group has id 1. Instead, each active legacy tenant becomes:

* a new member Group `tenant-<slug>` whose sandbox policy is a tenant boundary, and a `Tenant` row
  with the same id as that Group (the Group reserves the id, so nothing else resolves to it);
* when it had admins, an admin Group `tenant-<slug>-admins` pointing at the same `Tenant`, with
  `is_tenant_admin_group` set;
* group sandbox policies with the legacy `idp_groups` / `admin_idp_groups`, so SSO login keeps
  syncing membership (a revoked IdP group removes the user) instead of freezing it in rows.

Policies grant every machine, tag and route: the legacy tenancy had no resource limits.
Inactive legacy tenants get no Group. `LegacyTenantMap` records where each legacy id went;
`utils/tenancy_reconcile.py` uses it to rewrite the tasks stashed by alembic migration 6.

Runs only on genuine legacy state: `users_tenant` has rows and cape_tenancy is still empty.
An install that already configured cape_tenancy keeps its own model.
"""

import json

from django.conf import settings
from django.core.management.color import no_style
from django.db import migrations, models
import django.db.models.deletion

_ALL = ["*"]


def _rows(cursor, sql):
    cursor.execute(sql)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _json_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v) for v in value if str(v or "").strip()]


def _free_group_name(Group, alias, base):
    name = base[:150]
    n = 1
    while Group.objects.using(alias).filter(name=name).exists():
        suffix = "-legacy" if n == 1 else f"-legacy-{n}"
        name = base[: 150 - len(suffix)] + suffix
        n += 1
    return name


def _policy(Policy, alias, group, tenant, *, admin, idp_groups):
    return Policy.objects.using(alias).create(
        group=group,
        tenant=tenant,
        is_tenant_boundary=True,
        is_tenant_admin_group=admin,
        priority=100,
        allowed_machines=list(_ALL),
        allowed_tags=list(_ALL),
        allowed_routes=list(_ALL),
        allow_all_vms=True,
        allow_interactive=True,
        idp_groups=idp_groups,
        active=True,
    )


def import_legacy_tenancy(apps, schema_editor):
    connection = schema_editor.connection
    alias = connection.alias
    tables = set(connection.introspection.table_names())
    if "users_tenant" not in tables:
        return
    Tenant = apps.get_model("cape_tenancy", "Tenant")
    Policy = apps.get_model("cape_tenancy", "GroupSandboxPolicy")
    Membership = apps.get_model("cape_tenancy", "UserTenantMembership")
    LegacyTenantMap = apps.get_model("cape_tenancy", "LegacyTenantMap")
    Group = apps.get_model("auth", "Group")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))

    with connection.cursor() as cursor:
        legacy_tenants = _rows(
            cursor, "SELECT id, slug, name, idp_groups, admin_idp_groups, active FROM users_tenant ORDER BY id"
        )
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

    if not legacy_tenants:
        return
    if any(m.objects.using(alias).exists() for m in (Tenant, Policy, Membership, LegacyTenantMap)):
        # Not a first upgrade: cape_tenancy is already configured, and importing now would add
        # a second tenant to users the operator already placed.
        return

    existing_users = set(User.objects.using(alias).values_list("pk", flat=True))
    members = {}
    for row in profiles:
        if row["user_id"] in existing_users:
            members.setdefault(row["tenant_id"], []).append((row["user_id"], bool(row["is_tenant_admin"])))

    created = False
    for row in legacy_tenants:
        if not row["active"]:
            LegacyTenantMap.objects.using(alias).create(legacy_tenant_id=row["id"], tenant=None)
            continue
        slug = row["slug"]
        group = Group.objects.using(alias).create(name=_free_group_name(Group, alias, f"tenant-{slug}"))
        tenant = Tenant(
            id=group.id,
            slug=slug,
            name=row["name"] or slug,
            idp_groups=_json_list(row["idp_groups"]),
            admin_idp_groups=_json_list(row["admin_idp_groups"]),
            active=True,
        )
        tenant.save(using=alias, force_insert=True)
        created = True
        _policy(Policy, alias, group, tenant, admin=False, idp_groups=tenant.idp_groups)

        tenant_members = members.get(row["id"], [])
        admin_ids = [uid for uid, is_admin in tenant_members if is_admin]
        admin_group = None
        if admin_ids or tenant.admin_idp_groups:
            admin_group = Group.objects.using(alias).create(name=_free_group_name(Group, alias, f"tenant-{slug}-admins"))
            _policy(Policy, alias, admin_group, tenant, admin=True, idp_groups=tenant.admin_idp_groups)

        for user in User.objects.using(alias).filter(pk__in=[uid for uid, _ in tenant_members]):
            user.groups.add(group)
            if admin_group is not None and user.pk in admin_ids:
                user.groups.add(admin_group)
        LegacyTenantMap.objects.using(alias).create(legacy_tenant_id=row["id"], tenant=tenant)

    if created:
        # Tenant ids were set explicitly (to their Group's id): move the sequence past them.
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), [Tenant]):
                cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [
        ("cape_tenancy", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="LegacyTenantMap",
            fields=[
                ("legacy_tenant_id", models.IntegerField(primary_key=True, serialize=False)),
                (
                    "tenant",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="legacy_ids",
                        to="cape_tenancy.tenant",
                    ),
                ),
            ],
        ),
        migrations.RunPython(import_legacy_tenancy, migrations.RunPython.noop),
    ]
