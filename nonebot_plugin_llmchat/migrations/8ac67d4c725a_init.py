"""初始化会话状态表

Revision ID: 8ac67d4c725a
Revises:
Create Date: 2026-09-02
"""

from alembic import op
import sqlalchemy as sa

revision = "8ac67d4c725a"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nonebot_plugin_llmchat_conversation_state",
        sa.Column("context_id", sa.BigInteger(), nullable=False),
        sa.Column("is_group", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("context_id", "is_group"),
    )


def downgrade() -> None:
    op.drop_table("nonebot_plugin_llmchat_conversation_state")
