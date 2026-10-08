"""device login and fitness sync

Revision ID: c4e81f2a9d10
Revises: a7c31502a91e
Create Date: 2026-10-08 15:00:00+08:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c4e81f2a9d10"
down_revision = "a7c31502a91e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_authorizations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_code", sa.String(length=16), nullable=False, unique=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("scopes", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_device_authorizations_expires_at", "device_authorizations", ["expires_at"])
    op.create_table(
        "device_tokens",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("scopes", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_device_tokens_user_id", "device_tokens", ["user_id"])
    op.create_table(
        "fitness_profiles",
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "fitness_sessions",
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("session_id", sa.String(length=160), primary_key=True),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("started_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_fitness_sessions_changed", "fitness_sessions", ["user_id", "changed_at"])
    op.create_table(
        "fitness_checkins",
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("day", sa.String(length=10), primary_key=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("fitness_checkins")
    op.drop_index("ix_fitness_sessions_changed", table_name="fitness_sessions")
    op.drop_table("fitness_sessions")
    op.drop_table("fitness_profiles")
    op.drop_index("ix_device_tokens_user_id", table_name="device_tokens")
    op.drop_table("device_tokens")
    op.drop_index("ix_device_authorizations_expires_at", table_name="device_authorizations")
    op.drop_table("device_authorizations")
