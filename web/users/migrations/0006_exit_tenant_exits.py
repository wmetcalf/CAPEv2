from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("users", "0005_tenant_retention_days")]

    operations = [
        migrations.CreateModel(
            name="Exit",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.SlugField(max_length=48, unique=True)),
                ("name", models.CharField(blank=True, max_length=128)),
                ("is_global", models.BooleanField(default=False)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddField(
            model_name="tenant",
            name="exits",
            field=models.ManyToManyField(blank=True, related_name="tenants", to="users.exit"),
        ),
    ]
