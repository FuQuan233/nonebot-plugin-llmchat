"""新增全局共享表情包库

Revision ID: e13c2f0a9b7d
Revises: b21f89e4c6d0
Create Date: 2026-09-02
"""

from alembic import op
import sqlalchemy as sa

revision = "e13c2f0a9b7d"
down_revision = "b21f89e4c6d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nonebot_plugin_llmchat_sticker",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("file_name", sa.String(length=128), nullable=False),
        sa.Column("mime_type", sa.String(length=64), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("emotion", sa.String(length=64), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("recognized_text", sa.Text(), nullable=True),
        sa.Column("source_context_id", sa.BigInteger(), nullable=False),
        sa.Column("source_is_group", sa.Boolean(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("source_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("last_used_at", sa.Float(), nullable=True),
        sa.Column("use_count", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_name"),
        sa.UniqueConstraint("sha256"),
    )
    op.create_index(
        "ix_llmchat_sticker_enabled_emotion",
        "nonebot_plugin_llmchat_sticker",
        ["enabled", "emotion"],
    )


def downgrade() -> None:
    op.drop_index("ix_llmchat_sticker_enabled_emotion", table_name="nonebot_plugin_llmchat_sticker")
    op.drop_table("nonebot_plugin_llmchat_sticker")
