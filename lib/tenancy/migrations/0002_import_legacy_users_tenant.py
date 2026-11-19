"""Turn the fork's legacy `users_tenant` / `users_userprofile` tenancy into Django Groups.

Deployments that ran the old inline tenancy kept tenants in `users_tenant` and each user's
tenant in `users_userprofile.tenant_id` / `is_tenant_admin`. Their ids cannot be reused here:
tenant ids now share one id space with Django Group ids, so a legacy tenant 1 would merge with
whatever Group has id 1. Instead, each active legacy tenant becomes:

* a new member Group `tenant-<slug>` whose sandbox policy is a tenant boundary, and a `Tenant` row
  with the same id as that Group (the Group reserves the id, so nothing else resolves to it);
* when it had admins, an admin Group `tenant-<slug>-admins` pointing at the same `Tenant`, with
  `is_tenant_admin_group` set;
* `users_userprofile.is_tenant_admin` is dropped once read (nothing writes it any more);
* the member policy carries the legacy `idp_groups`, so SSO login keeps syncing membership (a
  revoked IdP group removes the user) instead of freezing it in rows.

The admin Group's policy has no `idp_groups`: the legacy rule granted admin only for a member
match AND an `admin_idp_groups` match, which a policy's `idp_groups` (any one claim) cannot
express. Legacy local admins are imported into it, and for a tenant with `idp_groups` SSO login
then applies the legacy rule to that one Group (`resources.reconcile_legacy_admin_groups`): kept
or added only while the user is in the member Group and claims an `admin_idp_groups` entry.

New Groups get ids above every existing Group id and every legacy tenant id ever issued (the
`users_tenant` sequence and any id still stamped in the alembic stash, which covers deleted
tenants), so until `utils/tenancy_reconcile.py` restamps them, no report still carrying a legacy
tenant id can match a new tenant.

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


def _legacy_id_high_water(connection, tables):
    """Highest legacy tenant id ever issued, including deleted tenants (0 if unknown)."""
    from django.db import transaction

    probes = []
    if connection.vendor == "sqlite":
        probes.append("SELECT seq FROM sqlite_sequence WHERE name = 'users_tenant'")
    elif connection.vendor == "postgresql":
        # The sequence actually owned by users_tenant.id (search_path-resolved, serial or identity).
        probes.append("SELECT pg_sequence_last_value(pg_get_serial_sequence('users_tenant', 'id')::regclass)")
    elif connection.vendor == "mysql":
        # MySQL 8 caches information_schema AUTO_INCREMENT (information_schema_stats_expiry).
        probes.append("SET SESSION information_schema_stats_expiry = 0")
        probes.append(
            "SELECT AUTO_INCREMENT - 1 FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users_tenant'"
        )
    if "tasks_legacy_tenancy" in tables:  # alembic stash, when it shares this database
        probes.append("SELECT MAX(tenant_id) FROM tasks_legacy_tenancy")
    high = 0
    for sql in probes:
        try:
            with transaction.atomic(using=connection.alias), connection.cursor() as cursor:
                cursor.execute(sql)
                row = cursor.fetchone() if cursor.description else None
        except Exception as exc:
            if sql.startswith("SET "):
                continue  # MariaDB / MySQL 5.x have no stats cache to bypass
            print(f"\ncape_tenancy: could not read a legacy tenant id high-water mark ({exc}); "
                  "utils/tenancy_reconcile.py refuses any tenant id left unmapped")
            continue
        if row and row[0] is not None:
            high = max(high, int(row[0]))
    return high


def _new_group(Group, alias, base, next_id):
    return Group.objects.using(alias).create(id=next_id, name=_free_group_name(Group, alias, base))


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
    _import(apps, schema_editor)
    _drop_legacy_admin_column(schema_editor.connection)


def _drop_legacy_admin_column(connection):
    """Drop users_userprofile.is_tenant_admin once read: the retired users.0004 left it NOT NULL
    with no default, and the current UserProfile model never writes it, so every new profile
    would fail to insert. This is the database that holds it (core may be a separate one)."""
    with connection.cursor() as cursor:
        if "users_userprofile" not in set(connection.introspection.table_names(cursor)):
            return
        cols = {c.name for c in connection.introspection.get_table_description(cursor, "users_userprofile")}
        if "is_tenant_admin" in cols:
            cursor.execute(
                f"ALTER TABLE {connection.ops.quote_name('users_userprofile')} "
                f"DROP COLUMN {connection.ops.quote_name('is_tenant_admin')}"
            )


def _import(apps, schema_editor):
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
        # a second tenant to users the operator already placed. utils/tenancy_reconcile.py then
        # finds no mapping for the stashed tenant ids and refuses rather than guessing.
        print(
            "\ncape_tenancy: users_tenant has legacy tenants but cape_tenancy is already configured; "
            "not importing them (see utils/tenancy_reconcile.py --drop-legacy-tenants)"
        )
        return

    existing_users = set(User.objects.using(alias).values_list("pk", flat=True))
    members = {}
    for row in profiles:
        if row["user_id"] in existing_users:
            members.setdefault(row["tenant_id"], []).append((row["user_id"], bool(row["is_tenant_admin"])))

    max_group = Group.objects.using(alias).aggregate(m=models.Max("id"))["m"] or 0
    next_id = max(max_group, max(row["id"] for row in legacy_tenants), _legacy_id_high_water(connection, tables)) + 1
    created = False
    for row in legacy_tenants:
        if not row["active"]:
            LegacyTenantMap.objects.using(alias).create(legacy_tenant_id=row["id"], tenant=None)
            continue
        slug = row["slug"]
        group = _new_group(Group, alias, f"tenant-{slug}", next_id)
        next_id += 1
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
            admin_group = _new_group(Group, alias, f"tenant-{slug}-admins", next_id)
            next_id += 1
            # Not IdP-managed: see the module docstring.
            _policy(Policy, alias, admin_group, tenant, admin=True, idp_groups=[])

        for user in User.objects.using(alias).filter(pk__in=[uid for uid, _ in tenant_members]):
            user.groups.add(group)
            if admin_group is not None and user.pk in admin_ids:
                user.groups.add(admin_group)
        LegacyTenantMap.objects.using(alias).create(
            legacy_tenant_id=row["id"], tenant=tenant, member_group=group, admin_group=admin_group
        )

    if created:
        # Group and Tenant ids were set explicitly: move both sequences past them.
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), [Group, Tenant]):
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
                (
                    "member_group",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="auth.group",
                    ),
                ),
                (
                    "admin_group",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="auth.group",
                    ),
                ),
            ],
        ),
        migrations.RunPython(import_legacy_tenancy, migrations.RunPython.noop),
    ]
