"""定时任务管理模块"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import aiofiles
import httpx
from nonebot import get_bot, get_driver, logger, require
from nonebot.adapters.onebot.v11 import Bot, Message
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as store

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler


class ScheduleType(str, Enum):
    """定时任务类型"""
    INTERVAL_MINUTES = "interval_minutes"  # 每N分钟
    DAILY = "daily"                        # 每天指定时间
    WEEKLY = "weekly"                      # 每周指定天
    YEARLY = "yearly"                      # 每年指定日期
    ONCE = "once"                          # 一次性任务


class ScheduledTask(BaseModel):
    """定时任务模型"""
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    context_id: int                         # 群号或用户ID
    is_group: bool                          # 是否群聊
    schedule_type: ScheduleType             # 任务类型
    description: str                        # 任务描述（用于AI生成提醒）
    creator_id: int                         # 创建者用户ID
    created_at: datetime = Field(default_factory=datetime.now)

    # 调度参数
    interval_minutes: int | None = None     # 间隔分钟数
    hour: int | None = None                 # 小时 (0-23)
    minute: int | None = None               # 分钟 (0-59)
    day_of_week: int | None = None          # 周几 (0-6, 0=周一)
    month: int | None = None                # 月份 (1-12)
    day: int | None = None                  # 日期 (1-31)

    # 一次性任务
    trigger_time: datetime | None = None    # 触发时间


class SchedulerTools:
    """定时任务工具定义"""

    def __init__(self):
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": "scheduler__create_task",
                    "description": """创建一个定时提醒任务。支持以下类型：
- interval_minutes: 每隔N分钟提醒，需提供 interval_minutes (1-10080)
- daily: 每天指定时间提醒，需提供 hour (0-23) 和 minute (0-59)
- weekly: 每周指定天提醒，需提供 hour, minute, day_of_week (0=周一, 1=周二...6=周日)
- yearly: 每年指定日期提醒，需提供 month (1-12), day (1-31), hour, minute
- once: 一次性提醒，需提供 minutes_later 表示几分钟后触发 (1-525600)""",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "schedule_type": {
                                "type": "string",
                                "description": "任务类型",
                                "enum": ["interval_minutes", "daily", "weekly", "yearly", "once"]
                            },
                            "description": {
                                "type": "string",
                                "description": "任务描述，将用于生成提醒信息"
                            },
                            "interval_minutes": {
                                "type": "integer",
                                "description": "间隔分钟数，仅interval_minutes类型需要",
                                "minimum": 1,
                                "maximum": 10080
                            },
                            "hour": {
                                "type": "integer",
                                "description": "小时 (0-23)",
                                "minimum": 0,
                                "maximum": 23
                            },
                            "minute": {
                                "type": "integer",
                                "description": "分钟 (0-59)",
                                "minimum": 0,
                                "maximum": 59
                            },
                            "day_of_week": {
                                "type": "integer",
                                "description": "周几 (0=周一, 1=周二...6=周日)",
                                "minimum": 0,
                                "maximum": 6
                            },
                            "month": {
                                "type": "integer",
                                "description": "月份 (1-12)",
                                "minimum": 1,
                                "maximum": 12
                            },
                            "day": {
                                "type": "integer",
                                "description": "日期 (1-31)",
                                "minimum": 1,
                                "maximum": 31
                            },
                            "minutes_later": {
                                "type": "integer",
                                "description": "几分钟后触发，仅once类型需要",
                                "minimum": 1,
                                "maximum": 525600
                            }
                        },
                        "required": ["schedule_type", "description"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "scheduler__list_tasks",
                    "description": "列出当前聊天的所有定时任务",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "scheduler__delete_task",
                    "description": "删除指定的定时任务",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "要删除的任务ID"
                            }
                        },
                        "required": ["task_id"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "scheduler__update_task",
                    "description": "更新定时任务的描述",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "要更新的任务ID"
                            },
                            "description": {
                                "type": "string",
                                "description": "新的任务描述"
                            }
                        },
                        "required": ["task_id", "description"]
                    }
                }
            }
        ]

    def get_available_tools(self) -> list[dict[str, Any]]:
        """获取可用的工具列表"""
        return self.tools

    def get_friendly_name(self, tool_name: str) -> str:
        """获取工具的友好名称"""
        friendly_names = {
            "scheduler__create_task": "定时任务 - 创建任务",
            "scheduler__list_tasks": "定时任务 - 列出任务",
            "scheduler__delete_task": "定时任务 - 删除任务",
            "scheduler__update_task": "定时任务 - 更新任务",
        }
        return friendly_names.get(tool_name, tool_name)


class SchedulerManager:
    """定时任务管理器"""

    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.tasks: dict[str, ScheduledTask] = {}
        self.tools = SchedulerTools()
        self.data_file = store.get_plugin_data_file("llmchat_scheduler_tasks.json")
        self._initialized = True
        logger.info("SchedulerManager 初始化完成")

    @classmethod
    def get_instance(cls) -> "SchedulerManager":
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def load_tasks(self):
        """从文件加载任务"""
        logger.info(f"从文件加载定时任务: {self.data_file}")
        if not os.path.exists(self.data_file):
            logger.debug("定时任务文件不存在，跳过加载")
            return

        try:
            async with aiofiles.open(self.data_file, encoding="utf8") as f:
                data = json.loads(await f.read())
                for task_id, task_data in data.items():
                    # 转换字符串为datetime
                    if task_data.get("created_at"):
                        task_data["created_at"] = datetime.fromisoformat(task_data["created_at"])
                    if task_data.get("trigger_time"):
                        task_data["trigger_time"] = datetime.fromisoformat(task_data["trigger_time"])
                    self.tasks[task_id] = ScheduledTask(**task_data)

            logger.info(f"成功加载 {len(self.tasks)} 个定时任务")
            # 注册所有任务到APScheduler
            await self.register_all_jobs()
        except Exception as e:
            logger.error(f"加载定时任务失败: {e}")

    async def save_tasks(self):
        """保存任务到文件"""
        logger.info(f"保存定时任务到文件: {self.data_file}")
        try:
            data = {}
            for task_id, task in self.tasks.items():
                task_dict = task.model_dump()
                # 转换datetime为字符串
                if task_dict.get("created_at"):
                    task_dict["created_at"] = task_dict["created_at"].isoformat()
                if task_dict.get("trigger_time"):
                    task_dict["trigger_time"] = task_dict["trigger_time"].isoformat()
                data[task_id] = task_dict

            os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
            async with aiofiles.open(self.data_file, "w", encoding="utf8") as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))
            logger.info(f"成功保存 {len(self.tasks)} 个定时任务")
        except Exception as e:
            logger.error(f"保存定时任务失败: {e}")

    def _validate_task_params(self, schedule_type: ScheduleType, **kwargs) -> str | None:
        """校验任务参数，返回错误信息或None"""
        if schedule_type == ScheduleType.INTERVAL_MINUTES:
            interval = kwargs.get("interval_minutes")
            if interval is None:
                return "interval_minutes类型需要提供 interval_minutes 参数"
            if not 1 <= interval <= 10080:
                return "interval_minutes 必须在 1-10080 之间"

        elif schedule_type == ScheduleType.DAILY:
            hour = kwargs.get("hour")
            minute = kwargs.get("minute")
            if hour is None or minute is None:
                return "daily类型需要提供 hour 和 minute 参数"
            if not 0 <= hour <= 23:
                return "hour 必须在 0-23 之间"
            if not 0 <= minute <= 59:
                return "minute 必须在 0-59 之间"

        elif schedule_type == ScheduleType.WEEKLY:
            hour = kwargs.get("hour")
            minute = kwargs.get("minute")
            day_of_week = kwargs.get("day_of_week")
            if hour is None or minute is None or day_of_week is None:
                return "weekly类型需要提供 hour, minute 和 day_of_week 参数"
            if not 0 <= hour <= 23:
                return "hour 必须在 0-23 之间"
            if not 0 <= minute <= 59:
                return "minute 必须在 0-59 之间"
            if not 0 <= day_of_week <= 6:
                return "day_of_week 必须在 0-6 之间 (0=周一)"

        elif schedule_type == ScheduleType.YEARLY:
            hour = kwargs.get("hour")
            minute = kwargs.get("minute")
            month = kwargs.get("month")
            day = kwargs.get("day")
            if hour is None or minute is None or month is None or day is None:
                return "yearly类型需要提供 hour, minute, month 和 day 参数"
            if not 0 <= hour <= 23:
                return "hour 必须在 0-23 之间"
            if not 0 <= minute <= 59:
                return "minute 必须在 0-59 之间"
            if not 1 <= month <= 12:
                return "month 必须在 1-12 之间"
            if not 1 <= day <= 31:
                return "day 必须在 1-31 之间"

        elif schedule_type == ScheduleType.ONCE:
            minutes_later = kwargs.get("minutes_later")
            if minutes_later is None:
                return "once类型需要提供 minutes_later 参数"
            if not 1 <= minutes_later <= 525600:
                return "minutes_later 必须在 1-525600 之间"

        return None

    async def create_task(
        self,
        context_id: int,
        is_group: bool,
        creator_id: int,
        schedule_type: str,
        description: str,
        **kwargs
    ) -> tuple[bool, str]:
        """创建定时任务"""
        try:
            stype = ScheduleType(schedule_type)
        except ValueError:
            return False, f"无效的任务类型: {schedule_type}"

        # 参数校验
        error = self._validate_task_params(stype, **kwargs)
        if error:
            return False, error

        # 计算一次性任务的触发时间
        trigger_time = None
        if stype == ScheduleType.ONCE:
            minutes_later = kwargs.get("minutes_later", 0)
            trigger_time = datetime.now() + timedelta(minutes=minutes_later)

        # 创建任务
        task = ScheduledTask(
            context_id=context_id,
            is_group=is_group,
            schedule_type=stype,
            description=description,
            creator_id=creator_id,
            interval_minutes=kwargs.get("interval_minutes"),
            hour=kwargs.get("hour"),
            minute=kwargs.get("minute"),
            day_of_week=kwargs.get("day_of_week"),
            month=kwargs.get("month"),
            day=kwargs.get("day"),
            trigger_time=trigger_time
        )

        self.tasks[task.task_id] = task

        # 注册到APScheduler
        self._register_job(task)

        # 保存
        await self.save_tasks()

        logger.info(f"创建定时任务成功: {task.task_id} - {description}")
        return True, f"创建成功！任务ID: {task.task_id}"

    async def delete_task(self, task_id: str, context_id: int, is_group: bool) -> tuple[bool, str]:
        """删除定时任务"""
        if task_id not in self.tasks:
            return False, f"任务不存在: {task_id}"

        task = self.tasks[task_id]

        # 检查权限：只能删除同一聊天的任务
        if task.context_id != context_id or task.is_group != is_group:
            return False, "无法删除其他聊天的任务"

        # 从APScheduler移除
        job_id = f"scheduler_{task_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

        # 删除任务
        del self.tasks[task_id]
        await self.save_tasks()

        logger.info(f"删除定时任务: {task_id}")
        return True, f"任务 {task_id} 已删除"

    async def update_task(
        self,
        task_id: str,
        context_id: int,
        is_group: bool,
        description: str
    ) -> tuple[bool, str]:
        """更新定时任务"""
        if task_id not in self.tasks:
            return False, f"任务不存在: {task_id}"

        task = self.tasks[task_id]

        # 检查权限
        if task.context_id != context_id or task.is_group != is_group:
            return False, "无法更新其他聊天的任务"

        task.description = description
        await self.save_tasks()

        logger.info(f"更新定时任务: {task_id}")
        return True, f"任务 {task_id} 已更新"

    def list_tasks(self, context_id: int, is_group: bool) -> list[dict]:
        """列出指定聊天的所有任务"""
        result = []
        for task in self.tasks.values():
            if task.context_id == context_id and task.is_group == is_group:
                task_info = {
                    "task_id": task.task_id,
                    "description": task.description,
                    "schedule_type": task.schedule_type.value,
                    "created_at": task.created_at.strftime("%Y-%m-%d %H:%M:%S")
                }

                # 添加具体时间信息
                if task.schedule_type == ScheduleType.INTERVAL_MINUTES:
                    task_info["schedule"] = f"每 {task.interval_minutes} 分钟"
                elif task.schedule_type == ScheduleType.DAILY:
                    task_info["schedule"] = f"每天 {task.hour:02d}:{task.minute:02d}"
                elif task.schedule_type == ScheduleType.WEEKLY:
                    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                    task_info["schedule"] = f"每{weekdays[task.day_of_week]} {task.hour:02d}:{task.minute:02d}"
                elif task.schedule_type == ScheduleType.YEARLY:
                    task_info["schedule"] = f"每年 {task.month}月{task.day}日 {task.hour:02d}:{task.minute:02d}"
                elif task.schedule_type == ScheduleType.ONCE:
                    if task.trigger_time:
                        task_info["schedule"] = f"一次性: {task.trigger_time.strftime('%Y-%m-%d %H:%M:%S')}"

                result.append(task_info)

        return result

    def _register_job(self, task: ScheduledTask):
        """注册单个任务到APScheduler"""
        job_id = f"scheduler_{task.task_id}"

        # 移除已存在的同ID任务
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

        if task.schedule_type == ScheduleType.INTERVAL_MINUTES:
            scheduler.add_job(
                self._trigger_task,
                "interval",
                minutes=task.interval_minutes,
                id=job_id,
                args=[task.task_id]
            )

        elif task.schedule_type == ScheduleType.DAILY:
            scheduler.add_job(
                self._trigger_task,
                "cron",
                hour=task.hour,
                minute=task.minute,
                id=job_id,
                args=[task.task_id]
            )

        elif task.schedule_type == ScheduleType.WEEKLY:
            # APScheduler的day_of_week: 0=周一...6=周日
            scheduler.add_job(
                self._trigger_task,
                "cron",
                day_of_week=task.day_of_week,
                hour=task.hour,
                minute=task.minute,
                id=job_id,
                args=[task.task_id]
            )

        elif task.schedule_type == ScheduleType.YEARLY:
            scheduler.add_job(
                self._trigger_task,
                "cron",
                month=task.month,
                day=task.day,
                hour=task.hour,
                minute=task.minute,
                id=job_id,
                args=[task.task_id]
            )

        elif task.schedule_type == ScheduleType.ONCE:
            if task.trigger_time and task.trigger_time > datetime.now():
                scheduler.add_job(
                    self._trigger_task,
                    "date",
                    run_date=task.trigger_time,
                    id=job_id,
                    args=[task.task_id]
                )

        logger.debug(f"注册定时任务到APScheduler: {job_id}")

    async def register_all_jobs(self):
        """注册所有任务到APScheduler"""
        logger.info(f"注册 {len(self.tasks)} 个任务到APScheduler")
        for task in self.tasks.values():
            self._register_job(task)

    async def _trigger_task(self, task_id: str):
        """任务触发处理"""
        if task_id not in self.tasks:
            logger.warning(f"触发的任务不存在: {task_id}")
            return

        task = self.tasks[task_id]
        logger.info(f"定时任务触发: {task_id} - {task.description}")

        # 导入配置（避免循环导入）
        from .config import ScopedConfig
        from nonebot import get_plugin_config
        from .config import Config
        plugin_config = get_plugin_config(Config).llmchat

        # 获取Bot
        try:
            bots = list(get_driver().bots.values())
            if not bots:
                logger.error("没有可用的Bot")
                return
            bot: Bot = bots[0]  # type: ignore
        except Exception as e:
            logger.error(f"获取Bot失败: {e}")
            return

        # 尝试调用AI生成提醒信息
        reminder_message = await self._generate_ai_reminder(task, plugin_config)

        # 发送消息
        try:
            if task.is_group:
                await bot.send_group_msg(group_id=task.context_id, message=Message(reminder_message))
            else:
                await bot.send_private_msg(user_id=task.context_id, message=Message(reminder_message))
            logger.info(f"定时任务提醒发送成功: {task_id}")
        except Exception as e:
            logger.error(f"发送提醒消息失败: {e}")

        # 一次性任务触发后删除
        if task.schedule_type == ScheduleType.ONCE:
            logger.info(f"删除一次性任务: {task_id}")
            del self.tasks[task_id]
            await self.save_tasks()

    async def _generate_ai_reminder(self, task: ScheduledTask, plugin_config) -> str:
        """调用AI生成提醒信息"""
        max_retry = plugin_config.scheduler_max_retry
        default_reminder = plugin_config.scheduler_default_reminder.format(description=task.description)

        # 获取预设配置
        preset = None
        if task.is_group:
            from . import group_states
            state = group_states.get(task.context_id)
            if state and state.preset_name != "off":
                for p in plugin_config.api_presets:
                    if p.name == state.preset_name:
                        preset = p
                        break
        else:
            from . import private_chat_states
            state = private_chat_states.get(task.context_id)
            if state and state.preset_name != "off":
                for p in plugin_config.api_presets:
                    if p.name == state.preset_name:
                        preset = p
                        break

        if not preset:
            # 没有配置预设，使用默认提醒
            logger.debug("没有可用的API预设，使用默认提醒")
            return default_reminder

        # 构建AI请求
        system_prompt = f"""你是一个友好的提醒助手。用户设置了一个定时提醒任务，现在任务触发了。
请根据任务描述生成一条简短、友好的提醒消息。
要求：
- 消息要简洁，不要太长
- 语气要友好、亲切
- 可以适当使用语气词或颜文字
- 不要有多余的解释，直接发送提醒内容"""

        user_prompt = f"任务描述：{task.description}"

        # 初始化OpenAI客户端
        if preset.proxy:
            client = AsyncOpenAI(
                base_url=preset.api_base,
                api_key=preset.api_key,
                timeout=plugin_config.request_timeout,
                http_client=httpx.AsyncClient(proxy=preset.proxy),
            )
        else:
            client = AsyncOpenAI(
                base_url=preset.api_base,
                api_key=preset.api_key,
                timeout=plugin_config.request_timeout,
            )

        for attempt in range(max_retry):
            try:
                response = await client.chat.completions.create(
                    model=preset.model_name,
                    max_tokens=256,
                    temperature=0.7,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ]
                )

                content = response.choices[0].message.content
                if content:
                    logger.debug(f"AI生成提醒成功: {content[:50]}...")
                    return content.strip()
            except Exception as e:
                logger.warning(f"AI生成提醒失败 (尝试 {attempt + 1}/{max_retry}): {e}")
                if attempt < max_retry - 1:
                    await asyncio.sleep(1)

        # 重试失败，返回默认提醒
        logger.warning(f"AI生成提醒全部失败，使用默认提醒")
        return default_reminder

    async def call_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        context_id: int,
        is_group: bool,
        creator_id: int
    ) -> str:
        """调用定时任务工具"""
        if tool_name == "scheduler__create_task":
            success, message = await self.create_task(
                context_id=context_id,
                is_group=is_group,
                creator_id=creator_id,
                schedule_type=tool_args.get("schedule_type", ""),
                description=tool_args.get("description", ""),
                interval_minutes=tool_args.get("interval_minutes"),
                hour=tool_args.get("hour"),
                minute=tool_args.get("minute"),
                day_of_week=tool_args.get("day_of_week"),
                month=tool_args.get("month"),
                day=tool_args.get("day"),
                minutes_later=tool_args.get("minutes_later")
            )
            return message

        elif tool_name == "scheduler__list_tasks":
            tasks = self.list_tasks(context_id, is_group)
            if not tasks:
                return "当前没有定时任务"
            return json.dumps(tasks, ensure_ascii=False, indent=2)

        elif tool_name == "scheduler__delete_task":
            success, message = await self.delete_task(
                task_id=tool_args.get("task_id", ""),
                context_id=context_id,
                is_group=is_group
            )
            return message

        elif tool_name == "scheduler__update_task":
            success, message = await self.update_task(
                task_id=tool_args.get("task_id", ""),
                context_id=context_id,
                is_group=is_group,
                description=tool_args.get("description", "")
            )
            return message

        return f"未知的工具: {tool_name}"

    def get_available_tools(self) -> list[dict[str, Any]]:
        """获取可用工具列表"""
        return self.tools.get_available_tools()

    def get_friendly_name(self, tool_name: str) -> str:
        """获取工具友好名称"""
        return self.tools.get_friendly_name(tool_name)
