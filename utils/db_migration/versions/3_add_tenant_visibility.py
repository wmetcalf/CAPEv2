"""add tenant_id + visibility to tasks

Multi-tenant identity & job-visibility foundation (spec #1). Adds the
per-task tenant owner and the 3-level visibility (public/tenant/private,
default private). Existing rows get visibility='private' via server_default
and tenant_id NULL (legacy/no-tenant) — the predicate treats those as
public/owner-only per spec §9 back-compat.

Revision ID: 3a1b_tenant_visibility
Revises: 2b3c4d5e6f7g
Create Date: 2026-06-05
"""
import sqlalchemy as sa
from alembic import op

revision = "3a1b_tenant_visibility"
down_revision = "2b3c4d5e6f7g"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("tenant_id", sa.Integer(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="private"),
    )
    op.create_index("ix_tasks_tenant_id", "tasks", ["tenant_id"])


def downgrade():
    op.drop_index("ix_tasks_tenant_id", table_name="tasks")
    op.drop_column("tasks", "visibility")
    op.drop_column("tasks", "tenant_id")
