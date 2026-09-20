"""player invite cooldown

Revision ID: a7c31502a91e
Revises: 1a649926182f
Create Date: 2026-09-20 18:30:00+08:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a7c31502a91e"
down_revision = "1a649926182f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("last_invite_created_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("last_invite_created_at")
