"""拆分会话元数据与对话轮次

Revision ID: b21f89e4c6d0
Revises: 8ac67d4c725a
Create Date: 2026-09-02
"""

import json
from typing import Any

from alembic import op
import sqlalchemy as sa

revision = "b21f89e4c6d0"
down_revision = "8ac67d4c725a"
branch_labels = None
depends_on = None

old_state = sa.table(
    "nonebot_plugin_llmchat_conversation_state",
    sa.column("context_id", sa.BigInteger()),
    sa.column("is_group", sa.Boolean()),
    sa.column("payload", sa.JSON()),
)
conversation = sa.table(
    "nonebot_plugin_llmchat_conversation",
    sa.column("context_id", sa.BigInteger()),
    sa.column("is_group", sa.Boolean()),
    sa.column("preset_name", sa.String(255)),
    sa.column("memory_summary", sa.Text()),
    sa.column("last_prompt_tokens", sa.BigInteger()),
    sa.column("last_cached_tokens", sa.BigInteger()),
    sa.column("usage_preset_name", sa.String(255)),
    sa.column("last_active", sa.Float()),
    sa.column("prompt", sa.Text()),
    sa.column("output_reasoning_content", sa.Boolean()),
    sa.column("random_trigger_prob", sa.Float()),
    sa.column("next_turn_sequence", sa.BigInteger()),
)
conversation_turn = sa.table(
    "nonebot_plugin_llmchat_conversation_turn",
    sa.column("context_id", sa.BigInteger()),
    sa.column("is_group", sa.Boolean()),
    sa.column("sequence", sa.BigInteger()),
    sa.column("messages", sa.JSON()),
)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def upgrade() -> None:
    op.create_table(
        "nonebot_plugin_llmchat_conversation",
        sa.Column("context_id", sa.BigInteger(), nullable=False),
        sa.Column("is_group", sa.Boolean(), nullable=False),
        sa.Column("preset_name", sa.String(length=255), nullable=False),
        sa.Column("memory_summary", sa.Text(), nullable=True),
        sa.Column("last_prompt_tokens", sa.BigInteger(), nullable=True),
        sa.Column("last_cached_tokens", sa.BigInteger(), nullable=True),
        sa.Column("usage_preset_name", sa.String(length=255), nullable=True),
        sa.Column("last_active", sa.Float(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("output_reasoning_content", sa.Boolean(), nullable=False),
        sa.Column("random_trigger_prob", sa.Float(), nullable=True),
        sa.Column("next_turn_sequence", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("context_id", "is_group"),
    )
    op.create_table(
        "nonebot_plugin_llmchat_conversation_turn",
        sa.Column("context_id", sa.BigInteger(), nullable=False),
        sa.Column("is_group", sa.Boolean(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["context_id", "is_group"],
            [
                "nonebot_plugin_llmchat_conversation.context_id",
                "nonebot_plugin_llmchat_conversation.is_group",
            ],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("context_id", "is_group", "sequence"),
    )

    bind = op.get_bind()
    conversation_rows: list[dict[str, Any]] = []
    turn_rows: list[dict[str, Any]] = []
    for row in bind.execute(sa.select(old_state)).mappings():
        payload = _json_object(row["payload"])
        turns = payload.get("turns", [])
        if not isinstance(turns, list):
            turns = []
        context_id = row["context_id"]
        is_group = row["is_group"]
        conversation_rows.append(
            {
                "context_id": context_id,
                "is_group": is_group,
                "preset_name": payload.get("preset", ""),
                "memory_summary": payload.get("memory_summary"),
                "last_prompt_tokens": payload.get("last_prompt_tokens"),
                "last_cached_tokens": payload.get("last_cached_tokens"),
                "usage_preset_name": payload.get("usage_preset_name"),
                "last_active": payload.get("last_active", 0.0),
                "prompt": payload.get("group_prompt", payload.get("prompt")),
                "output_reasoning_content": payload.get("output_reasoning_content", False),
                "random_trigger_prob": payload.get("random_trigger_prob") if is_group else None,
                "next_turn_sequence": len(turns),
            }
        )
        turn_rows.extend(
            {
                "context_id": context_id,
                "is_group": is_group,
                "sequence": sequence,
                "messages": messages,
            }
            for sequence, messages in enumerate(turns)
            if isinstance(messages, list)
        )
    if conversation_rows:
        bind.execute(sa.insert(conversation), conversation_rows)
    if turn_rows:
        bind.execute(sa.insert(conversation_turn), turn_rows)
    op.drop_table("nonebot_plugin_llmchat_conversation_state")


def downgrade() -> None:
    op.create_table(
        "nonebot_plugin_llmchat_conversation_state",
        sa.Column("context_id", sa.BigInteger(), nullable=False),
        sa.Column("is_group", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("context_id", "is_group"),
    )

    bind = op.get_bind()
    turns_by_key: dict[tuple[int, bool], list[list[dict[str, Any]]]] = {}
    ordered_turns = sa.select(conversation_turn).order_by(
        conversation_turn.c.context_id,
        conversation_turn.c.is_group,
        conversation_turn.c.sequence,
    )
    for row in bind.execute(ordered_turns).mappings():
        key = (row["context_id"], row["is_group"])
        turns_by_key.setdefault(key, []).append(row["messages"])

    state_rows: list[dict[str, Any]] = []
    for row in bind.execute(sa.select(conversation)).mappings():
        key = (row["context_id"], row["is_group"])
        payload = {
            "memory_version": 2,
            "preset": row["preset_name"],
            "memory_summary": row["memory_summary"],
            "turns": turns_by_key.get(key, []),
            "last_prompt_tokens": row["last_prompt_tokens"],
            "last_cached_tokens": row["last_cached_tokens"],
            "usage_preset_name": row["usage_preset_name"],
            "last_active": row["last_active"],
            "group_prompt": row["prompt"],
            "output_reasoning_content": row["output_reasoning_content"],
        }
        if row["is_group"]:
            payload["random_trigger_prob"] = row["random_trigger_prob"]
        state_rows.append(
            {
                "context_id": row["context_id"],
                "is_group": row["is_group"],
                "payload": payload,
            }
        )
    if state_rows:
        bind.execute(sa.insert(old_state), state_rows)
    op.drop_table("nonebot_plugin_llmchat_conversation_turn")
    op.drop_table("nonebot_plugin_llmchat_conversation")
