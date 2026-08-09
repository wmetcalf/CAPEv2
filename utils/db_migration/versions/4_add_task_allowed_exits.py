"""add allowed_exits to tasks

Tenant-scoped egress exits (spec: tenant-egress-exits). Stores the CSV of egress
exit slugs a task's tenant may use, stamped at submit from allowed_exit_slugs(viewer).

Back-compat: NULL == unrestricted. Existing rows and any task submitted with
multitenancy disabled or in shared mode stay NULL, so the worker's route_network
guard is a no-op for them (today's behavior). The column is only populated for
tasks submitted under locked-mode multitenancy.

Revision ID: 4a2b_task_allowed_exits
Revises: 3a1b_tenant_visibility
Create Date: 2026-07-09
"""
import sqlalchemy as sa
from alembic import op

revision = "4a2b_task_allowed_exits"
down_revision = "3a1b_tenant_visibility"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("allowed_exits", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("tasks", "allowed_exits")
