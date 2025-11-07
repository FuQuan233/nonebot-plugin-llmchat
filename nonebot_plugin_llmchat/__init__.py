import asyncio
import base64
from collections import defaultdict, deque
from datetime import datetime
import json
import os
import random
import re
import ssl
import time
from typing import TYPE_CHECKING

import aiofiles
import httpx
from nonebot import (
    get_bot,
    get_driver,
    get_plugin_config,
    logger,
    on_command,
    on_message,
    require,
)
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from openai import AsyncOpenAI

from .config import Config, PresetConfig
from .mcpclient import MCPClient

require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as store

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

if TYPE_CHECKING:
    from openai.types.chat import (
        ChatCompletionContentPartParam,
        ChatCompletionMessageParam,
    )

__plugin_meta__ = PluginMetadata(
    name="llmchat",
    description="支持多API预设、MCP协议、联网搜索、视觉模型、Nano Banana（生图模型）的AI群聊插件",
    usage="""@机器人 + 消息 开启对话""",
    type="application",
    homepage="https://github.com/FuQuan233/nonebot-plugin-llmchat",
    config=Config,
    supported_adapters={"~onebot.v11"},
)

plugin_config = get_plugin_config(Config).llmchat
driver = get_driver()
tasks: set["asyncio.Task"] = set()


def pop_reasoning_content(
    content: str | None,
) -> tuple[str | None, str | None]:
    if content is None:
        return None, None

    # 如果找到了 <think> 标签内容，返回过滤后的文本和标签内的内容，否则只返回过滤后的文本和None
    if matched := re.match(r"<think>(.*?)</think>", content, flags=re.DOTALL):
        reasoning_element = matched.group(0)
        reasoning_content = matched.group(1).strip()
        filtered_content = content.replace(reasoning_element, "").strip()

        return filtered_content, reasoning_content
    else:
        return content, None


# 初始化群组状态
class GroupState:
    def __init__(self):
        self.preset_name = plugin_config.default_preset
        self.history = deque(maxlen=plugin_config.history_size * 2)
        self.queue = asyncio.Queue()
        self.processing = False
        self.last_active = time.time()
        self.past_events = deque(maxlen=plugin_config.past_events_size)
        self.group_prompt: str | None = None
        self.user_prompt: str | None = None
        self.output_reasoning_content = False
        self.random_trigger_prob = plugin_config.random_trigger_prob


# 初始化私聊状态
class PrivateChatState:
    def __init__(self):
        self.preset_name = plugin_config.private_chat_preset
        self.history = deque(maxlen=plugin_config.history_size * 2)
        self.queue = asyncio.Queue()
        self.processing = False
        self.last_active = time.time()
        self.past_events = deque(maxlen=plugin_config.past_events_size)
        self.group_prompt: str | None = None
        self.user_prompt: str | None = None
        self.output_reasoning_content = False


group_states: dict[int, GroupState] = defaultdict(GroupState)
private_chat_states: dict[int, PrivateChatState] = defaultdict(PrivateChatState)


# 获取当前预设配置
def get_preset(context_id: int, is_group: bool = True) -> PresetConfig:
    if is_group:
        state = group_states[context_id]
    else:
        state = private_chat_states[context_id]

    for preset in plugin_config.api_presets:
        if preset.name == state.preset_name:
            return preset
    return plugin_config.api_presets[0]  # 默认返回第一个预设


# 消息格式转换
def format_message(event: GroupMessageEvent | PrivateMessageEvent) -> str:
    text_message = ""
    if isinstance(event, GroupMessageEvent) and event.reply is not None:
        text_message += f"[回复 {event.reply.sender.nickname} 的消息 {event.reply.message.extract_plain_text()}]\n"

    if isinstance(event, GroupMessageEvent) and event.is_tome():
        text_message += f"@{next(iter(driver.config.nickname))} "

    for msgseg in event.get_message():
        if msgseg.type == "at":
            text_message += msgseg.data.get("name", "")
        elif msgseg.type == "image":
            text_message += "[图片]"
        elif msgseg.type == "voice":
            text_message += "[语音]"
        elif msgseg.type == "face":
            pass
        elif msgseg.type == "text":
            text_message += msgseg.data.get("text", "")

    if isinstance(event, GroupMessageEvent):
        message = {
            "SenderNickname": str(event.sender.card or event.sender.nickname),
            "SenderUserId": str(event.user_id),
            "Message": text_message,
            "MessageID": event.message_id,
            "SendTime": datetime.fromtimestamp(event.time).isoformat(),
        }
    else:  # PrivateMessageEvent
        message = {
            "SenderNickname": str(event.sender.nickname),
            "SenderUserId": str(event.user_id),
            "Message": text_message,
            "MessageID": event.message_id,
            "SendTime": datetime.fromtimestamp(event.time).isoformat(),
        }
    return json.dumps(message, ensure_ascii=False)


def build_reasoning_forward_nodes(self_id: str, reasoning_content: str):
    self_nickname = next(iter(driver.config.nickname))
    nodes = [
        {
            "type": "node",
            "data": {
                "nickname": self_nickname,
                "user_id": self_id,
                "content": f"{self_nickname}的内心OS:",
            },
        },
        {
            "type": "node",
            "data": {
                "nickname": self_nickname,
                "user_id": self_id,
                "content": reasoning_content,
            },
        },
    ]

    return nodes


async def is_triggered(event: GroupMessageEvent | PrivateMessageEvent) -> bool:
    """扩展后的消息处理规则"""

    if isinstance(event, GroupMessageEvent):
        state = group_states[event.group_id]

        if state.preset_name == "off":
            return False

        # 黑名单用户
        if event.user_id in plugin_config.blacklist_user_ids:
            return False

        # 忽略特定前缀的消息
        msg_text = event.get_plaintext().strip()
        for prefix in plugin_config.ignore_prefixes:
            if msg_text.startswith(prefix):
                return False

        state.past_events.append(event)

        # 原有@触发条件
        if event.is_tome():
            return True

        # 随机触发条件
        if random.random() < state.random_trigger_prob:
            return True

        return False

    elif isinstance(event, PrivateMessageEvent):
        # 检查私聊功能是否启用
        if not plugin_config.enable_private_chat:
            return False

        state = private_chat_states[event.user_id]

        if state.preset_name == "off":
            return False

        # 黑名单用户
        if event.user_id in plugin_config.blacklist_user_ids:
            return False

        # 忽略特定前缀的消息
        msg_text = event.get_plaintext().strip()
        for prefix in plugin_config.ignore_prefixes:
            if msg_text.startswith(prefix):
                return False

        state.past_events.append(event)

        # 私聊默认触发
        return True

    return False


# 消息处理器
handler = on_message(
    rule=Rule(is_triggered),
    priority=99,
    block=False,
)


@handler.handle()
async def handle_message(event: GroupMessageEvent | PrivateMessageEvent):
    if isinstance(event, GroupMessageEvent):
        group_id = event.group_id
        logger.debug(
            f"收到群聊消息 群号：{group_id} 用户：{event.user_id} 内容：{event.get_plaintext()}"
        )
        state = group_states[group_id]
        context_id = group_id
    else:  # PrivateMessageEvent
        user_id = event.user_id
        logger.debug(
            f"收到私聊消息 用户：{user_id} 内容：{event.get_plaintext()}"
        )
        state = private_chat_states[user_id]
        context_id = user_id

    await state.queue.put(event)
    if not state.processing:
        state.processing = True
        is_group = isinstance(event, GroupMessageEvent)
        task = asyncio.create_task(process_messages(context_id, is_group))
        task.add_done_callback(tasks.discard)
        tasks.add(task)

async def process_images(event: GroupMessageEvent) -> list[str]:
    base64_images = []
    for segement in event.get_message():
        if segement.type == "image":
            image_url = segement.data.get("url") or segement.data.get("file")
            if image_url:
                try:
                    # 处理高版本 httpx 的 [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] 报错
                    ssl_context = ssl.create_default_context()
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
                    ssl_context.set_ciphers("DEFAULT@SECLEVEL=2")

                    # 下载图片并将图片转换为base64
                    async with httpx.AsyncClient(verify=ssl_context) as client:
                        response = await client.get(image_url, timeout=10.0)
                        if response.status_code != 200:
                            logger.error(f"下载图片失败: {image_url}, 状态码: {response.status_code}")
                            continue
                        image_data = response.content
                        base64_data = base64.b64encode(image_data).decode("utf-8")
                        base64_images.append(base64_data)
                except Exception as e:
                    logger.error(f"处理图片时出错: {e}")
    logger.debug(f"共处理 {len(base64_images)} 张图片")
    return base64_images

async def send_split_messages(message_handler, content: str):
    """
    将消息按分隔符<botbr>分段并发送
    """
    logger.info(f"准备发送分段消息，分段数：{len(content.split('<botbr>'))}")
    for segment in content.split("<botbr>"):
        # 跳过空消息
        if not segment.strip():
            continue
        segment = segment.strip()  # 删除前后多余的换行和空格
        await asyncio.sleep(2)  # 避免发送过快
        logger.debug(f"发送消息分段 内容：{segment[:50]}...")  # 只记录前50个字符避免日志过大
        await message_handler.send(Message(segment))

async def process_messages(context_id: int, is_group: bool = True):
    if is_group:
        group_id = context_id
        state = group_states[group_id]
    else:
        user_id = context_id
        state = private_chat_states[user_id]
        group_id = None

    preset = get_preset(context_id, is_group)

    # 初始化OpenAI客户端
    if preset.proxy != "":
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

    chat_type = "群聊" if is_group else "私聊"
    context_type = "群号" if is_group else "用户"
    logger.info(
        f"开始处理{chat_type}消息 {context_type}：{context_id} 当前队列长度：{state.queue.qsize()}"
    )
    while not state.queue.empty():
        event = await state.queue.get()
        if is_group:
            logger.debug(f"从队列获取消息 群号：{context_id} 消息ID：{event.message_id}")
            group_id = context_id
        else:
            logger.debug(f"从队列获取消息 用户：{context_id} 消息ID：{event.message_id}")
            group_id = None
        past_events_snapshot = []
        mcp_client = MCPClient.get_instance(plugin_config.mcp_servers)
        try:
            # 构建系统提示，分成多行以满足行长限制
            chat_type = "群聊" if is_group else "私聊"
            bot_names = "、".join(list(driver.config.nickname))
            default_prompt = (state.group_prompt if is_group else state.user_prompt) or plugin_config.default_prompt

            system_lines = [
                f"我想要你帮我在{chat_type}中闲聊，大家一般叫你{bot_names}。",
                "我将会在后面的信息中告诉你每条信息的发送者和发送时间，你可以直接称呼发送者为他对应的昵称。",
                "你的回复需要遵守以下几点规则：",
                "- 你可以使用多条消息回复，每两条消息之间使用<botbr>分隔，<botbr>前后不需要包含额外的换行和空格。",
                "- 除<botbr>外，消息中不应该包含其他类似的标记。",
                "- 不要使用markdown或者html，聊天软件不支持解析，换行请用换行符。",
                "- 你应该以普通人的方式发送消息，每条消息字数要尽量少一些，但是不建议超过两条。",
                "- 代码则不需要分段，用单独的一条消息发送。",
                "- 请使用发送者的昵称称呼发送者，你可以礼貌地问候发送者，但只需要在第一次回答这位发送者的问题时问候他。",
                "- 你有at群成员的能力，只需要在某条消息中插入[CQ:at,qq=（QQ号）]，"
                "也就是CQ码。at发送者是非必要的，如果不是必要，请不要at别人。",
                "- 你有引用某条消息的能力，使用[CQ:reply,id=（消息id）]来引用。",
                "- 如果有多条消息，你应该优先回复提到你的，一段时间之前的就不要回复了，也可以直接选择不回复。",
                "- 如果你选择完全不回复，你只需要直接输出一个<botbr>。",
                "- 如果你需要思考的话，你应该尽量少思考，以节省时间。",
                "下面是关于你性格的设定，如果设定中提到让你扮演某个人，或者设定中有提到名字，则优先使用设定中的名字。",
                default_prompt,
            ]

            systemPrompt = "\n".join(system_lines)
            if preset.support_mcp:
                systemPrompt += "你也可以使用一些工具，下面是关于这些工具的额外说明：\n"
                for mcp_name, mcp_config in plugin_config.mcp_servers.items():
                    if mcp_config.addtional_prompt:
                        systemPrompt += f"{mcp_name}：{mcp_config.addtional_prompt}"
                        systemPrompt += "\n"

            messages: list[ChatCompletionMessageParam] = [
                {"role": "system", "content": systemPrompt}
            ]

            while len(state.history) > 0 and state.history[0]["role"] != "user":
                state.history.popleft()

            messages += list(state.history)[-plugin_config.history_size * 2 :]

            # 没有未处理的消息说明已经被处理了，跳过
            if state.past_events.__len__() < 1:
                break

            content: list[ChatCompletionContentPartParam] = []

            # 将机器人错过的消息推送给LLM
            past_events_snapshot = list(state.past_events)
            state.past_events.clear()
            for ev in past_events_snapshot:
                text_content = format_message(ev)
                content.append({"type": "text", "text": text_content})

                # 将消息中的图片转成 base64
                if preset.support_image:
                    base64_images = await process_images(ev)
                    for base64_image in base64_images:
                        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}})

            new_messages: list[ChatCompletionMessageParam] = [
                {"role": "user", "content": content}
            ]

            logger.debug(
                f"发送API请求 模型：{preset.model_name} 历史消息数：{len(messages)}"
            )

            client_config = {
                "model": preset.model_name,
                "max_tokens": preset.max_tokens,
                "temperature": preset.temperature,
                "timeout": 60,
            }

            if preset.support_mcp:
                available_tools = await mcp_client.get_available_tools()
                client_config["tools"] = available_tools

            response = await client.chat.completions.create(
                **client_config,
                messages=messages + new_messages,
            )

            if response.usage is not None:
                logger.debug(f"收到API响应 使用token数：{response.usage.total_tokens}")

            message = response.choices[0].message

            # 处理响应并处理工具调用
            while preset.support_mcp and message and message.tool_calls:
                new_messages.append({
                    "role": "assistant",
                    "tool_calls": [tool_call.model_dump() for tool_call in message.tool_calls]
                })

                # 发送LLM调用工具时的回复，一般没有
                if message.content:
                    await send_split_messages(handler, message.content)

                # 处理每个工具调用
                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments)

                    # 发送工具调用提示
                    await handler.send(Message(f"正在使用{mcp_client.get_friendly_name(tool_name)}"))

                    # 执行工具调用，传递群组和机器人信息用于QQ工具
                    if is_group:
                        result = await mcp_client.call_tool(
                            tool_name,
                            tool_args,
                            group_id=event.group_id,
                            bot_id=str(event.self_id),
                            user_id=event.user_id
                        )
                    else:
                        # 私聊时某些工具不可用（如群操作工具），跳过这些工具
                        if tool_name.startswith("ob__"):
                            result = f"私聊不支持{mcp_client.get_friendly_name(tool_name)}工具"
                        else:
                            result = await mcp_client.call_tool(
                                tool_name,
                                tool_args,
                                group_id=None,
                                bot_id=str(event.self_id),
                                user_id=event.user_id
                            )

                    new_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": str(result)
                    })

                # 将工具调用的结果交给 LLM
                response = await client.chat.completions.create(
                    **client_config,
                    messages=messages + new_messages,
                )

                message = response.choices[0].message

            # 安全检查：确保 message 不为 None
            if not message:
                logger.error("API 响应中的 message 为 None")
                await handler.send(Message("服务暂时不可用，请稍后再试"))
                return

            reply, matched_reasoning_content = pop_reasoning_content(
                message.content
            )
            reasoning_content: str | None = (
                getattr(message, "reasoning_content", None)
                or matched_reasoning_content
            )

            llm_reply: ChatCompletionMessageParam = {
                "role": "assistant",
                "content": reply,
            }

            reply_images = getattr(message, "images", None)

            if reply_images:
                # openai的sdk里的assistant消息暂时没有images字段，需要单独处理
                llm_reply["images"] = reply_images # pyright: ignore[reportGeneralTypeIssues]

            new_messages.append(llm_reply)

            # 请求成功后再保存历史记录，保证user和assistant穿插，防止R1模型报错
            for message in new_messages:
                state.history.append(message)

            if state.output_reasoning_content and reasoning_content:
                try:
                    bot = get_bot(str(event.self_id))
                    await bot.send_group_forward_msg(
                        group_id=group_id,
                        messages=build_reasoning_forward_nodes(
                            bot.self_id, reasoning_content
                        ),
                    )
                except Exception as e:
                    logger.error(f"合并转发消息发送失败：\n{e!s}\n")

            assert reply is not None
            await send_split_messages(handler, reply)

            if reply_images:
                logger.debug(f"API响应 图片数：{len(reply_images)}")
                for i, image in enumerate(reply_images, start=1):
                    logger.debug(f"正在发送第{i}张图片")
                    image_base64 = image["image_url"]["url"].removeprefix("data:image/png;base64,")
                    image_msg = MessageSegment.image(base64.b64decode(image_base64))
                    await handler.send(image_msg)

        except Exception as e:
            logger.opt(exception=e).error(f"API请求失败 {'群号' if is_group else '用户'}：{context_id}")
            # 如果在处理过程中出现异常，恢复未处理的消息到state中
            state.past_events.extendleft(reversed(past_events_snapshot))
            await handler.send(Message(f"服务暂时不可用，请稍后再试\n{e!s}"))
        finally:
            state.processing = False
            state.queue.task_done()
            # 不再需要每次都清理MCPClient，因为它现在是单例
            # await mcp_client.cleanup()


# 预设切换命令
preset_handler = on_command("API预设", priority=1, block=True, permission=SUPERUSER)


@preset_handler.handle()
async def handle_preset(event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id
    preset_name = args.extract_plain_text().strip()

    if preset_name == "off":
        group_states[group_id].preset_name = preset_name
        await preset_handler.finish("已关闭llmchat")

    available_presets = {p.name for p in plugin_config.api_presets}
    if preset_name not in available_presets:
        available_presets_str = "\n- ".join(available_presets)
        await preset_handler.finish(
            f"当前API预设：{group_states[group_id].preset_name}\n可用API预设：\n- {available_presets_str}"
        )

    group_states[group_id].preset_name = preset_name
    await preset_handler.finish(f"已切换至API预设：{preset_name}")


edit_preset_handler = on_command(
    "修改设定",
    priority=1,
    block=True,
    permission=(SUPERUSER | GROUP_ADMIN | GROUP_OWNER),
)


@edit_preset_handler.handle()
async def handle_edit_preset(event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id
    group_prompt = args.extract_plain_text().strip()

    group_states[group_id].group_prompt = group_prompt
    await edit_preset_handler.finish("修改成功")


reset_handler = on_command(
    "记忆清除",
    priority=1,
    block=True,
    permission=(SUPERUSER | GROUP_ADMIN | GROUP_OWNER),
)


@reset_handler.handle()
async def handle_reset(event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id

    group_states[group_id].past_events.clear()
    group_states[group_id].history.clear()
    await reset_handler.finish("记忆已清空")


set_prob_handler = on_command(
    "设置主动回复概率",
    priority=1,
    block=True,
    permission=(SUPERUSER | GROUP_ADMIN | GROUP_OWNER),
)


@set_prob_handler.handle()
async def handle_set_prob(event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id
    prob = 0

    try:
        prob = float(args.extract_plain_text().strip())
        if prob < 0 or prob > 1:
            raise ValueError
    except Exception as e:
        await reset_handler.finish(f"输入有误，请使用 [0,1] 的浮点数\n{e!s}")

    group_states[group_id].random_trigger_prob = prob
    await reset_handler.finish(f"主动回复概率已设为 {prob}")


# 预设切换命令
think_handler = on_command(
    "切换思维输出",
    priority=1,
    block=True,
    permission=(SUPERUSER | GROUP_ADMIN | GROUP_OWNER),
)


@think_handler.handle()
async def handle_think(event: GroupMessageEvent, args: Message = CommandArg()):
    state = group_states[event.group_id]
    state.output_reasoning_content = not state.output_reasoning_content

    await think_handler.finish(
        f"已{(state.output_reasoning_content and '开启') or '关闭'}思维输出"
    )


# region 私聊相关指令

# 私聊预设切换命令
private_preset_handler = on_command(
    "私聊API预设",
    priority=1,
    block=True,
    permission=SUPERUSER,
)


@private_preset_handler.handle()
async def handle_private_preset(event: PrivateMessageEvent, args: Message = CommandArg()):
    if not plugin_config.enable_private_chat:
        await private_preset_handler.finish("私聊功能未启用")

    user_id = event.user_id
    preset_name = args.extract_plain_text().strip()

    if preset_name == "off":
        private_chat_states[user_id].preset_name = preset_name
        await private_preset_handler.finish("已关闭llmchat私聊功能")

    available_presets = {p.name for p in plugin_config.api_presets}
    if preset_name not in available_presets:
        available_presets_str = "\n- ".join(available_presets)
        await private_preset_handler.finish(
            f"当前API预设：{private_chat_states[user_id].preset_name}\n可用API预设：\n- {available_presets_str}"
        )

    private_chat_states[user_id].preset_name = preset_name
    await private_preset_handler.finish(f"已切换至API预设：{preset_name}")


# 私聊设定修改命令
private_edit_preset_handler = on_command(
    "私聊修改设定",
    priority=1,
    block=True,
    permission=SUPERUSER,
)


@private_edit_preset_handler.handle()
async def handle_private_edit_preset(event: PrivateMessageEvent, args: Message = CommandArg()):
    if not plugin_config.enable_private_chat:
        await private_edit_preset_handler.finish("私聊功能未启用")

    user_id = event.user_id
    user_prompt = args.extract_plain_text().strip()

    private_chat_states[user_id].group_prompt = user_prompt
    await private_edit_preset_handler.finish("修改成功")


# 私聊记忆清除命令
private_reset_handler = on_command(
    "私聊记忆清除",
    priority=1,
    block=True,
    permission=SUPERUSER,
)


@private_reset_handler.handle()
async def handle_private_reset(event: PrivateMessageEvent, args: Message = CommandArg()):
    if not plugin_config.enable_private_chat:
        await private_reset_handler.finish("私聊功能未启用")

    user_id = event.user_id

    private_chat_states[user_id].past_events.clear()
    private_chat_states[user_id].history.clear()
    await private_reset_handler.finish("记忆已清空")


# 私聊思维输出切换命令
private_think_handler = on_command(
    "私聊切换思维输出",
    priority=1,
    block=True,
    permission=SUPERUSER,
)


@private_think_handler.handle()
async def handle_private_think(event: PrivateMessageEvent, args: Message = CommandArg()):
    if not plugin_config.enable_private_chat:
        await private_think_handler.finish("私聊功能未启用")

    state = private_chat_states[event.user_id]
    state.output_reasoning_content = not state.output_reasoning_content

    await private_think_handler.finish(
        f"已{(state.output_reasoning_content and '开启') or '关闭'}思维输出"
    )

# endregion


# region 持久化与定时任务

# 获取插件数据目录
data_dir = store.get_plugin_data_dir()
# 获取插件数据文件
data_file = store.get_plugin_data_file("llmchat_state.json")
private_data_file = store.get_plugin_data_file("llmchat_private_state.json")


async def save_state():
    """保存群组状态到文件"""
    logger.info(f"开始保存群组状态到文件：{data_file}")
    data = {
        gid: {
            "preset": state.preset_name,
            "history": list(state.history),
            "last_active": state.last_active,
            "group_prompt": state.group_prompt,
            "output_reasoning_content": state.output_reasoning_content,
            "random_trigger_prob": state.random_trigger_prob,
        }
        for gid, state in group_states.items()
    }

    os.makedirs(os.path.dirname(data_file), exist_ok=True)
    async with aiofiles.open(data_file, "w", encoding="utf8") as f:
        await f.write(json.dumps(data, ensure_ascii=False))

    # 保存私聊状态
    if plugin_config.enable_private_chat:
        logger.info(f"开始保存私聊状态到文件：{private_data_file}")
        private_data = {
            uid: {
                "preset": state.preset_name,
                "history": list(state.history),
                "last_active": state.last_active,
                "group_prompt": state.group_prompt,
                "output_reasoning_content": state.output_reasoning_content,
            }
            for uid, state in private_chat_states.items()
        }

        os.makedirs(os.path.dirname(private_data_file), exist_ok=True)
        async with aiofiles.open(private_data_file, "w", encoding="utf8") as f:
            await f.write(json.dumps(private_data, ensure_ascii=False))


async def load_state():
    """从文件加载群组状态"""
    logger.info(f"从文件加载群组状态：{data_file}")
    if not os.path.exists(data_file):
        return

    async with aiofiles.open(data_file, encoding="utf8") as f:
        data = json.loads(await f.read())
        for gid, state_data in data.items():
            state = GroupState()
            state.preset_name = state_data["preset"]
            state.history = deque(
                state_data["history"], maxlen=plugin_config.history_size * 2
            )
            state.last_active = state_data["last_active"]
            state.group_prompt = state_data["group_prompt"]
            state.output_reasoning_content = state_data["output_reasoning_content"]
            state.random_trigger_prob = state_data.get("random_trigger_prob", plugin_config.random_trigger_prob)
            group_states[int(gid)] = state

    # 加载私聊状态
    if plugin_config.enable_private_chat:
        logger.info(f"从文件加载私聊状态：{private_data_file}")
        if os.path.exists(private_data_file):
            async with aiofiles.open(private_data_file, encoding="utf8") as f:
                private_data = json.loads(await f.read())
                for uid, state_data in private_data.items():
                    state = PrivateChatState()
                    state.preset_name = state_data["preset"]
                    state.history = deque(
                        state_data["history"], maxlen=plugin_config.history_size * 2
                    )
                    state.last_active = state_data["last_active"]
                    state.group_prompt = state_data["group_prompt"]
                    state.output_reasoning_content = state_data["output_reasoning_content"]
                    private_chat_states[int(uid)] = state


# 注册生命周期事件
@driver.on_startup
async def init_plugin():
    logger.info("插件启动初始化")
    await load_state()
    # 每5分钟保存状态
    scheduler.add_job(save_state, "interval", minutes=5)


@driver.on_shutdown
async def cleanup_plugin():
    logger.info("插件关闭清理")
    await save_state()
    # 销毁MCPClient单例
    await MCPClient.destroy_instance()
