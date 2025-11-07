"""
Tortoise ORM 模型定义
用于存储聊天历史和群组/私聊状态
"""
import json

from nonebot_plugin_tortoise_orm import add_model
from tortoise import fields
from tortoise.models import Model

# 注册模型到 Tortoise ORM
add_model(__name__)


class GroupChatState(Model):
    """群组聊天状态"""

    id = fields.IntField(pk=True)
    group_id = fields.BigIntField(unique=True, description="群号")
    preset_name = fields.CharField(max_length=50, description="当前使用的 API 预设名")
    group_prompt = fields.TextField(null=True, description="群组自定义提示词")
    output_reasoning_content = fields.BooleanField(default=False, description="是否输出推理内容")
    random_trigger_prob = fields.FloatField(default=0.05, description="随机触发概率")
    last_active = fields.DatetimeField(auto_now=True, description="最后活跃时间")
    created_at = fields.DatetimeField(auto_now_add=True, description="创建时间")

    class Meta:
        table = "llmchat_group_state"
        table_description = "群组聊天状态表"


class PrivateChatState(Model):
    """私聊状态"""

    id = fields.IntField(pk=True)
    user_id = fields.BigIntField(unique=True, description="用户 QQ")
    preset_name = fields.CharField(max_length=50, description="当前使用的 API 预设名")
    user_prompt = fields.TextField(null=True, description="用户自定义提示词")
    output_reasoning_content = fields.BooleanField(default=False, description="是否输出推理内容")
    last_active = fields.DatetimeField(auto_now=True, description="最后活跃时间")
    created_at = fields.DatetimeField(auto_now_add=True, description="创建时间")

    class Meta:
        table = "llmchat_private_state"
        table_description = "私聊状态表"


class ChatMessage(Model):
    """聊天消息历史"""

    id = fields.IntField(pk=True)
    group_id = fields.BigIntField(null=True, description="群号（私聊时为 NULL）")
    user_id = fields.BigIntField(null=True, description="用户 QQ（私聊时有值）")
    is_private = fields.BooleanField(default=False, description="是否为私聊")
    role = fields.CharField(
        max_length=20,
        description="消息角色: user/assistant/system/tool",
    )
    content = fields.TextField(description="消息内容（JSON 序列化）")
    created_at = fields.DatetimeField(auto_now_add=True, description="消息时间")

    class Meta:
        table = "llmchat_message"
        table_description = "聊天消息历史表"

    @staticmethod
    def serialize_content(content) -> str:
        """将内容序列化为 JSON 字符串"""
        return json.dumps(content, ensure_ascii=False)

    @staticmethod
    def deserialize_content(content_str: str):
        """从 JSON 字符串反序列化内容"""
        return json.loads(content_str)


class ChatHistory(Model):
    """聊天历史快照（用于快速加载）"""

    id = fields.IntField(pk=True)
    group_id = fields.BigIntField(null=True, unique=True, description="群号（私聊时为 NULL）")
    user_id = fields.BigIntField(null=True, unique=True, description="用户 QQ（私聊时有值）")
    is_private = fields.BooleanField(default=False, description="是否为私聊")
    # 存储最近 history_size*2 条消息的 JSON 数组
    messages_json = fields.TextField(description="消息历史（JSON 数组）")
    last_update = fields.DatetimeField(auto_now=True, description="最后更新时间")

    class Meta:
        table = "llmchat_history"
        table_description = "聊天历史快照表（用于快速加载）"

    @staticmethod
    def serialize_messages(messages_list) -> str:
        """将消息列表序列化为 JSON 字符串"""
        return json.dumps(messages_list, ensure_ascii=False)

    @staticmethod
    def deserialize_messages(messages_json: str):
        """从 JSON 字符串反序列化消息列表"""
        return json.loads(messages_json)
