"""drop inline tenant_id + visibility columns from tasks if present

Idempotently removes inline tasks.tenant_id and tasks.visibility columns on deployments
that previously applied 3a1b_tenant_visibility before multi-tenancy moved out of core.

Before dropping them, every task that carries tenancy information (a tenant_id or a
visibility) is copied verbatim into the `tasks_legacy_tenancy` side
table. Core does not interpret those values: the old tenant ids belong to a Tenant table
that no longer exists, so mapping them is left to whichever tenancy plugin is installed
(`lib.tenancy` consumes and drops the table in `utils/tenancy_reconcile.py`).
Deployments that never had the inline columns are untouched and get no side table.

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

LEGACY_TABLE = "tasks_legacy_tenancy"


def _stash_inline_tenancy(bind, insp, columns):
    """Copy inline tenancy into LEGACY_TABLE so dropping the columns loses nothing."""
    if "visibility" not in columns and "tenant_id" not in columns:
        return
    if LEGACY_TABLE not in insp.get_table_names():
        op.create_table(
            LEGACY_TABLE,
            sa.Column("task_id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("tenant_id", sa.Integer(), nullable=True),
            sa.Column("visibility", sa.String(16), nullable=True),
        )
    tenant = "t.tenant_id" if "tenant_id" in columns else "NULL"
    visibility = "t.visibility" if "visibility" in columns else "NULL"
    carries = []
    if "tenant_id" in columns:
        carries.append("t.tenant_id IS NOT NULL")
    if "visibility" in columns:
        # Private too: an explicit record keeps a stale report stamp from overriding it later.
        carries.append("t.visibility IS NOT NULL")
    bind.execute(
        sa.text(
            f"INSERT INTO {LEGACY_TABLE} (task_id, tenant_id, visibility) "
            f"SELECT t.id, {tenant}, {visibility} FROM tasks t "
            f"WHERE ({' OR '.join(carries)}) "
            f"AND NOT EXISTS (SELECT 1 FROM {LEGACY_TABLE} l WHERE l.task_id = t.id)"
        )
    )


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    columns = {col["name"] for col in insp.get_columns("tasks")}
    _stash_inline_tenancy(bind, insp, columns)
    indexes = {ix["name"] for ix in insp.get_indexes("tasks")}
    if "ix_tasks_tenant_id" in indexes:
        op.drop_index("ix_tasks_tenant_id", table_name="tasks")
    if "visibility" in columns:
        op.drop_column("tasks", "visibility")
    if "tenant_id" in columns:
        op.drop_column("tasks", "tenant_id")


def downgrade():
    pass
