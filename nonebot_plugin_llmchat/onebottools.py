import json
import time
from typing import Any, cast

from nonebot import get_bot, logger
from nonebot.adapters.onebot.v11 import Bot


class OneBotTools:
    """内置的OneBot群操作工具类"""

    def __init__(self):
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": "ob__mute_user",
                    "description": "禁言指定用户一段时间。需要机器人有管理员权限。不要随便禁言别人，你应该只听群主或者管理员你的话。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "user_id": {"type": "string", "description": "要禁言的用户QQ号"},
                            "duration": {
                                "type": "integer",
                                "description": "禁言时长（秒），0表示解除禁言，最大2592000（30天）",
                                "minimum": 0,
                                "maximum": 2592000,
                            },
                        },
                        "required": ["user_id", "duration"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ob__get_group_info",
                    "description": "获取群信息，包括群成员数量、群名称等。",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ob__get_group_member_info",
                    "description": "获取指定群成员的信息。",
                    "parameters": {
                        "type": "object",
                        "properties": {"user_id": {"type": "string", "description": "要查询的用户QQ号"}},
                        "required": ["user_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ob__get_group_member_list",
                    "description": "获取群成员列表。",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ob__poke_user",
                    "description": "戳一戳指定用户。",
                    "parameters": {
                        "type": "object",
                        "properties": {"user_id": {"type": "string", "description": "要戳一戳的用户QQ号"}},
                        "required": ["user_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ob__recall_message",
                    "description": "撤回指定消息。需要机器人有管理员权限或者是消息发送者。",
                    "parameters": {
                        "type": "object",
                        "properties": {"message_id": {"type": "integer", "description": "要撤回的消息ID"}},
                        "required": ["message_id"],
                    },
                },
            },
        ]

    def get_friendly_name(self, tool_name: str) -> str:
        """获取工具的友好名称"""
        friendly_names = {
            "ob__mute_user": "OneBot - 禁言用户",
            "ob__get_group_info": "OneBot - 获取群信息",
            "ob__get_group_member_info": "OneBot - 获取成员信息",
            "ob__get_group_member_list": "OneBot - 获取成员列表",
            "ob__poke_user": "OneBot - 戳一戳用户",
            "ob__recall_message": "OneBot - 撤回消息",
        }
        return friendly_names.get(tool_name, tool_name)

    def get_available_tools(self) -> list[dict[str, Any]]:
        """获取可用的工具列表"""
        return self.tools

    async def call_tool(self, tool_name: str, tool_args: dict[str, Any], group_id: int, bot_id: str) -> str:
        """调用指定的工具"""
        try:
            bot = cast(Bot, get_bot(bot_id))

            if tool_name == "ob__mute_user":
                return await self._mute_user(bot, group_id, tool_args)
            elif tool_name == "ob__get_group_info":
                return await self._get_group_info(bot, group_id, tool_args)
            elif tool_name == "ob__get_group_member_info":
                return await self._get_group_member_info(bot, group_id, tool_args)
            elif tool_name == "ob__get_group_member_list":
                return await self._get_group_member_list(bot, group_id, tool_args)
            elif tool_name == "ob__poke_user":
                return await self._poke_user(bot, group_id, tool_args)
            elif tool_name == "ob__recall_message":
                return await self._recall_message(bot, group_id, tool_args)
            else:
                return f"未知的工具: {tool_name}"

        except Exception as e:
            logger.error(f"调用OneBot工具 {tool_name} 时出错: {e}")
            return f"执行失败: {e!s}"

    async def _mute_user(self, bot: Bot, group_id: int, args: dict[str, Any]) -> str:
        """禁言用户"""
        user_id = int(args["user_id"])
        duration = args["duration"]

        try:
            await bot.set_group_ban(group_id=group_id, user_id=user_id, duration=duration)
            if duration > 0:
                return f"成功禁言用户 {user_id}，时长 {duration} 秒"
            else:
                return f"成功解除用户 {user_id} 的禁言"
        except Exception as e:
            return f"禁言操作失败: {e!s}"

    async def _get_group_info(self, bot: Bot, group_id: int, _args: dict[str, Any]) -> str:
        """获取群信息"""
        try:
            group_info = await bot.get_group_info(group_id=group_id)
            info = {
                "群号": group_info["group_id"],
                "群名称": group_info["group_name"],
                "群成员数": group_info["member_count"],
                "群上限": group_info["max_member_count"],
            }
            return json.dumps(info, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"获取群信息失败: {e!s}"

    async def _get_group_member_info(self, bot: Bot, group_id: int, args: dict[str, Any]) -> str:
        """获取群成员信息"""
        user_id = int(args["user_id"])

        try:
            member_info = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
            info = {
                "用户QQ": member_info["user_id"],
                "昵称": member_info["nickname"],
                "群名片": member_info["card"],
                "性别": member_info["sex"],
                "年龄": member_info["age"],
                "地区": member_info["area"],
                "加群时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(member_info["join_time"])),
                "最后发言时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(member_info["last_sent_time"])),
                "群内等级": member_info["level"],
                "角色": member_info["role"],
                "专属头衔": member_info["title"],
            }
            return json.dumps(info, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"获取成员信息失败: {e!s}"

    async def _get_group_member_list(self, bot: Bot, group_id: int, _args: dict[str, Any]) -> str:
        """获取群成员列表"""
        try:
            member_list = await bot.get_group_member_list(group_id=group_id)
            members = []
            for member in member_list:
                members.append(
                    {"QQ": member["user_id"], "昵称": member["nickname"], "群名片": member["card"], "角色": member["role"]}
                )

            result = {"群成员总数": len(members), "成员列表": members}
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"获取群成员列表失败: {e!s}"

    async def _poke_user(self, bot: Bot, group_id: int, args: dict[str, Any]) -> str:
        """戳一戳用户"""
        user_id = int(args["user_id"])

        try:
            # 使用OneBot的戳一戳API
            await bot.call_api("group_poke", group_id=group_id, user_id=user_id)
            return f"成功戳了戳用户 {user_id}"
        except Exception as e:
            return f"戳一戳失败: {e!s}"

    async def _recall_message(self, bot: Bot, group_id: int, args: dict[str, Any]) -> str:
        """撤回消息"""
        message_id = int(args["message_id"])

        try:
            await bot.delete_msg(message_id=message_id)
            return f"成功撤回消息 {message_id}"
        except Exception as e:
            return f"撤回消息失败: {e!s}"


