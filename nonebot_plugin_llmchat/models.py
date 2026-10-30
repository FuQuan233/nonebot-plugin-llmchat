from typing import Any

from nonebot_plugin_datastore import get_plugin_data
from sqlalchemy import JSON, BigInteger, Boolean, Float, ForeignKeyConstraint, Index, String, Text
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


class StickerRecord(Model):
    """所有会话共享的表情包及模型生成的语义索引。"""

    __tablename__ = "nonebot_plugin_llmchat_sticker"
    __table_args__ = (Index("ix_llmchat_sticker_enabled_emotion", "enabled", "emotion"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    file_name: Mapped[str] = mapped_column(String(128), unique=True)
    mime_type: Mapped[str] = mapped_column(String(64))
    file_size: Mapped[int] = mapped_column(BigInteger)
    description: Mapped[str] = mapped_column(Text)
    emotion: Mapped[str] = mapped_column(String(64))
    keywords: Mapped[list[str]] = mapped_column(JSON)
    recognized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_context_id: Mapped[int] = mapped_column(BigInteger)
    source_is_group: Mapped[bool] = mapped_column(Boolean)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    source_user_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[float] = mapped_column(Float)
    last_used_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    use_count: Mapped[int] = mapped_column(BigInteger)
    enabled: Mapped[bool] = mapped_column(Boolean)
