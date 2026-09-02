from typing import Any

from nonebot_plugin_datastore import get_plugin_data
from sqlalchemy import JSON, BigInteger, Boolean, Float, ForeignKeyConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

plugin_data = get_plugin_data("nonebot_plugin_llmchat")
Model = plugin_data.Model


class ConversationRecord(Model):
    """群聊或私聊的会话元数据。"""

    __tablename__ = "nonebot_plugin_llmchat_conversation"

    context_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    is_group: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    preset_name: Mapped[str] = mapped_column(String(255))
    memory_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_prompt_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_cached_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    usage_preset_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_active: Mapped[float] = mapped_column(Float)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_reasoning_content: Mapped[bool] = mapped_column(Boolean)
    random_trigger_prob: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_turn_sequence: Mapped[int] = mapped_column(BigInteger)


class ConversationTurnRecord(Model):
    """一个不可拆分的完整对话轮次。"""

    __tablename__ = "nonebot_plugin_llmchat_conversation_turn"
    __table_args__ = (
        ForeignKeyConstraint(
            ["context_id", "is_group"],
            [
                "nonebot_plugin_llmchat_conversation.context_id",
                "nonebot_plugin_llmchat_conversation.is_group",
            ],
            ondelete="CASCADE",
        ),
    )

    context_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    is_group: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
