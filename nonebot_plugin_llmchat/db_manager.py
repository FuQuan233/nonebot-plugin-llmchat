"""
数据库操作层
处理聊天历史和状态的持久化
"""
import json
from collections import deque
from datetime import datetime
from typing import Optional

from nonebot import logger
from tortoise.exceptions import DoesNotExist

from .models import ChatHistory, ChatMessage, GroupChatState, PrivateChatState


class DatabaseManager:
    """数据库管理器"""

    @staticmethod
    async def save_group_state(
        group_id: int,
        preset_name: str,
        history: deque,
        group_prompt: Optional[str],
        output_reasoning_content: bool,
        random_trigger_prob: float,
    ):
        """保存群组状态和历史到数据库"""
        try:
            # 保存或更新群组状态
            state, _ = await GroupChatState.get_or_create(
                group_id=group_id,
                defaults={
                    "preset_name": preset_name,
                    "group_prompt": group_prompt,
                    "output_reasoning_content": output_reasoning_content,
                    "random_trigger_prob": random_trigger_prob,
                },
            )
            if _:  # 如果是新创建的
                logger.debug(f"创建群组状态记录: {group_id}")
            else:
                # 更新现有记录
                state.preset_name = preset_name
                state.group_prompt = group_prompt
                state.output_reasoning_content = output_reasoning_content
                state.random_trigger_prob = random_trigger_prob
                await state.save()
                logger.debug(f"更新群组状态记录: {group_id}")

            # 保存历史快照
            messages_list = list(history)
            history_record, _ = await ChatHistory.get_or_create(
                group_id=group_id,
                is_private=False,
                defaults={"messages_json": ChatHistory.serialize_messages(messages_list)},
            )
            if not _:
                history_record.messages_json = ChatHistory.serialize_messages(messages_list)
                await history_record.save()

            logger.debug(f"已保存群组 {group_id} 的历史记录（{len(messages_list)} 条消息）")

        except Exception as e:
            logger.error(f"保存群组状态失败 群号: {group_id}, 错误: {e}")

    @staticmethod
    async def save_private_state(
        user_id: int,
        preset_name: str,
        history: deque,
        user_prompt: Optional[str],
        output_reasoning_content: bool,
    ):
        """保存私聊状态和历史到数据库"""
        try:
            # 保存或更新私聊状态
            state, _ = await PrivateChatState.get_or_create(
                user_id=user_id,
                defaults={
                    "preset_name": preset_name,
                    "user_prompt": user_prompt,
                    "output_reasoning_content": output_reasoning_content,
                },
            )
            if _:  # 如果是新创建的
                logger.debug(f"创建私聊状态记录: {user_id}")
            else:
                # 更新现有记录
                state.preset_name = preset_name
                state.user_prompt = user_prompt
                state.output_reasoning_content = output_reasoning_content
                await state.save()
                logger.debug(f"更新私聊状态记录: {user_id}")

            # 保存历史快照
            messages_list = list(history)
            history_record, _ = await ChatHistory.get_or_create(
                user_id=user_id,
                is_private=True,
                defaults={"messages_json": ChatHistory.serialize_messages(messages_list)},
            )
            if not _:
                history_record.messages_json = ChatHistory.serialize_messages(messages_list)
                await history_record.save()

            logger.debug(f"已保存用户 {user_id} 的历史记录（{len(messages_list)} 条消息）")

        except Exception as e:
            logger.error(f"保存私聊状态失败 用户: {user_id}, 错误: {e}")

    @staticmethod
    async def load_group_state(group_id: int, history_maxlen: int) -> dict:
        """从数据库加载群组状态"""
        try:
            state = await GroupChatState.get_or_none(group_id=group_id)
            if not state:
                logger.debug(f"未找到群组 {group_id} 的状态记录，返回默认值")
                return None

            # 加载历史
            history_record = await ChatHistory.get_or_none(
                group_id=group_id, is_private=False
            )
            history = deque(
                ChatHistory.deserialize_messages(history_record.messages_json)
                if history_record
                else [],
                maxlen=history_maxlen,
            )

            logger.debug(f"已加载群组 {group_id} 的状态（{len(history)} 条历史）")

            return {
                "preset_name": state.preset_name,
                "history": history,
                "group_prompt": state.group_prompt,
                "output_reasoning_content": state.output_reasoning_content,
                "random_trigger_prob": state.random_trigger_prob,
                "last_active": state.last_active.timestamp(),
            }

        except Exception as e:
            logger.error(f"加载群组状态失败 群号: {group_id}, 错误: {e}")
            return None

    @staticmethod
    async def load_private_state(user_id: int, history_maxlen: int) -> dict:
        """从数据库加载私聊状态"""
        try:
            state = await PrivateChatState.get_or_none(user_id=user_id)
            if not state:
                logger.debug(f"未找到用户 {user_id} 的状态记录，返回默认值")
                return None

            # 加载历史
            history_record = await ChatHistory.get_or_none(
                user_id=user_id, is_private=True
            )
            history = deque(
                ChatHistory.deserialize_messages(history_record.messages_json)
                if history_record
                else [],
                maxlen=history_maxlen,
            )

            logger.debug(f"已加载用户 {user_id} 的状态（{len(history)} 条历史）")

            return {
                "preset_name": state.preset_name,
                "history": history,
                "user_prompt": state.user_prompt,
                "output_reasoning_content": state.output_reasoning_content,
                "last_active": state.last_active.timestamp(),
            }

        except Exception as e:
            logger.error(f"加载私聊状态失败 用户: {user_id}, 错误: {e}")
            return None

    @staticmethod
    async def load_all_group_states(history_maxlen: int) -> dict:
        """加载所有群组状态"""
        try:
            states = await GroupChatState.all()
            result = {}

            for state in states:
                history_record = await ChatHistory.get_or_none(
                    group_id=state.group_id, is_private=False
                )
                history = deque(
                    ChatHistory.deserialize_messages(history_record.messages_json)
                    if history_record
                    else [],
                    maxlen=history_maxlen,
                )

                result[state.group_id] = {
                    "preset_name": state.preset_name,
                    "history": history,
                    "group_prompt": state.group_prompt,
                    "output_reasoning_content": state.output_reasoning_content,
                    "random_trigger_prob": state.random_trigger_prob,
                    "last_active": state.last_active.timestamp(),
                }

            logger.info(f"已加载 {len(result)} 个群组的状态")
            return result

        except Exception as e:
            logger.error(f"加载所有群组状态失败, 错误: {e}")
            return {}

    @staticmethod
    async def load_all_private_states(history_maxlen: int) -> dict:
        """加载所有私聊状态"""
        try:
            states = await PrivateChatState.all()
            result = {}

            for state in states:
                history_record = await ChatHistory.get_or_none(
                    user_id=state.user_id, is_private=True
                )
                history = deque(
                    ChatHistory.deserialize_messages(history_record.messages_json)
                    if history_record
                    else [],
                    maxlen=history_maxlen,
                )

                result[state.user_id] = {
                    "preset_name": state.preset_name,
                    "history": history,
                    "user_prompt": state.user_prompt,
                    "output_reasoning_content": state.output_reasoning_content,
                    "last_active": state.last_active.timestamp(),
                }

            logger.info(f"已加载 {len(result)} 个用户的私聊状态")
            return result

        except Exception as e:
            logger.error(f"加载所有私聊状态失败, 错误: {e}")
            return {}

    @staticmethod
    async def clear_group_history(group_id: int):
        """清空群组历史"""
        try:
            await ChatHistory.filter(group_id=group_id, is_private=False).delete()
            state = await GroupChatState.get_or_none(group_id=group_id)
            if state:
                await state.delete()
            logger.info(f"已清空群组 {group_id} 的历史记录")
        except Exception as e:
            logger.error(f"清空群组历史失败 群号: {group_id}, 错误: {e}")

    @staticmethod
    async def clear_private_history(user_id: int):
        """清空私聊历史"""
        try:
            await ChatHistory.filter(user_id=user_id, is_private=True).delete()
            state = await PrivateChatState.get_or_none(user_id=user_id)
            if state:
                await state.delete()
            logger.info(f"已清空用户 {user_id} 的历史记录")
        except Exception as e:
            logger.error(f"清空私聊历史失败 用户: {user_id}, 错误: {e}")
