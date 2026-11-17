"""drop inline tenant_id + visibility columns from tasks if present

Idempotently removes inline tasks.tenant_id and tasks.visibility columns on deployments
that previously applied 3a1b_tenant_visibility before multi-tenancy moved out of core.

Before dropping them, any task that carries tenancy information (a tenant_id, or a
visibility other than private) is copied into the task_acl side table, unless that task
already has a task_acl row. Deployments that never had the inline columns are untouched.

Revision ID: 4c2d_drop_inline_tenancy
Revises: 4a6c2b_machine_attributes
Create Date: 2026-10-09
"""
import sqlalchemy as sa
from alembic import op

revision = "4c2d_drop_inline_tenancy"
down_revision = "4a6c2b_machine_attributes"
branch_labels = None
depends_on = None


def _carry_inline_tenancy_to_task_acl(bind, columns):
    """Copy inline tenancy into task_acl so dropping the columns loses nothing."""
    if "visibility" not in columns and "tenant_id" not in columns:
        return
    from lib.tenancy.acl import ensure_acl_schema

    ensure_acl_schema(bind)
    tenant = "t.tenant_id" if "tenant_id" in columns else "NULL"
    # Only private is the no-row default; anything unrecognized is carried as private (fail-closed).
    visibility = (
        "CASE WHEN t.visibility IN ('public', 'tenant') THEN t.visibility ELSE 'private' END"
        if "visibility" in columns
        else "'private'"
    )
    carries = []
    if "tenant_id" in columns:
        carries.append("t.tenant_id IS NOT NULL")
    if "visibility" in columns:
        carries.append("t.visibility IN ('public', 'tenant')")
    bind.execute(
        sa.text(
            "INSERT INTO task_acl (task_id, user_id, tenant_id, visibility, visibility_seq) "
            f"SELECT t.id, t.user_id, {tenant}, {visibility}, 1 FROM tasks t "
            f"WHERE ({' OR '.join(carries)}) "
            "AND NOT EXISTS (SELECT 1 FROM task_acl a WHERE a.task_id = t.id)"
        )
    )


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    _carry_inline_tenancy_to_task_acl(bind, {col["name"] for col in insp.get_columns("tasks")})
    indexes = {ix["name"] for ix in insp.get_indexes("tasks")}
    if "ix_tasks_tenant_id" in indexes:
        op.drop_index("ix_tasks_tenant_id", table_name="tasks")
    columns = {col["name"] for col in insp.get_columns("tasks")}
    if "visibility" in columns:
        op.drop_column("tasks", "visibility")
    if "tenant_id" in columns:
        op.drop_column("tasks", "tenant_id")


def downgrade():
    pass
