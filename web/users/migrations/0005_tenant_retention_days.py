from django.db import migrations, models


class Migration(migrations.Migration):
    """Add Tenant.retention_days (per-tenant central-store lifetime, days). The field was
    added to the model for the central-store retention timer but its migration was never
    committed — so a fresh migrate (CI / new deploy / test DB) lacked the column and every
    Tenant query raised 'no such column: users_tenant.retention_days'."""

    dependencies = [
        ("users", "0004_tenant_userprofile_is_tenant_admin_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="retention_days",
            field=models.PositiveIntegerField(default=90),
        ),
    ]
