import asyncio
from collections import defaultdict, deque
from datetime import datetime
import json
import os
import random
import re
import time
from typing import TYPE_CHECKING, Optional

import aiofiles
from nonebot import (
    get_bot,
    get_driver,
    get_plugin_config,
    logger,
    on_command,
    on_message,
    require,
)
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from openai import AsyncOpenAI

from .config import Config, PresetConfig

require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as store

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

if TYPE_CHECKING:
    from collections.abc import Iterable

    from openai.types.chat import ChatCompletionMessageParam

__plugin_meta__ = PluginMetadata(
    name="llmchat",
    description="支持多API预设配置的AI群聊插件",
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
    content: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
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
        self.history = deque(maxlen=plugin_config.history_size)
        self.queue = asyncio.Queue()
        self.processing = False
        self.last_active = time.time()
        self.past_events = deque(maxlen=plugin_config.past_events_size)
        self.group_prompt: Optional[str] = None
        self.output_reasoning_content = False
        self.random_trigger_prob = plugin_config.random_trigger_prob


group_states: dict[int, GroupState] = defaultdict(GroupState)


# 获取当前预设配置
def get_preset(group_id: int) -> PresetConfig:
    state = group_states[group_id]
    for preset in plugin_config.api_presets:
        if preset.name == state.preset_name:
            return preset
    return plugin_config.api_presets[0]  # 默认返回第一个预设


# 消息格式转换
def format_message(event: GroupMessageEvent) -> str:
    text_message = ""
    if event.reply is not None:
        text_message += f"[回复 {event.reply.sender.nickname} 的消息 {event.reply.message.extract_plain_text()}]\n"

    if event.is_tome():
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

    message = {
        "SenderNickname": str(event.sender.card or event.sender.nickname),
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


async def is_triggered(event: GroupMessageEvent) -> bool:
    """扩展后的消息处理规则"""

    state = group_states[event.group_id]

    if state.preset_name == "off":
        return False

    state.past_events.append(event)

    # 原有@触发条件
    if event.is_tome():
        return True

    # 随机触发条件
    if random.random() < plugin_config.random_trigger_prob:
        return True

    return False


# 消息处理器
handler = on_message(
    rule=Rule(is_triggered),
    priority=10,
    block=False,
)


@handler.handle()
async def handle_message(event: GroupMessageEvent):
    group_id = event.group_id
    logger.debug(
        f"收到群聊消息 群号：{group_id} 用户：{event.user_id} 内容：{event.get_plaintext()}"
    )

    state = group_states[group_id]

    await state.queue.put(event)
    if not state.processing:
        state.processing = True
        task = asyncio.create_task(process_messages(group_id))
        task.add_done_callback(tasks.discard)
        tasks.add(task)


async def process_messages(group_id: int):
    state = group_states[group_id]
    preset = get_preset(group_id)

    # 初始化OpenAI客户端
    client = AsyncOpenAI(
        base_url=preset.api_base,
        api_key=preset.api_key,
        timeout=plugin_config.request_timeout,
    )

    logger.info(
        f"开始处理群聊消息 群号：{group_id} 当前队列长度：{state.queue.qsize()}"
    )
    while not state.queue.empty():
        event = await state.queue.get()
        logger.debug(f"从队列获取消息 群号：{group_id} 消息ID：{event.message_id}")
        try:
            systemPrompt = f"""
我想要你帮我在群聊中闲聊，大家一般叫你{"、".join(list(driver.config.nickname))}，我将会在后面的信息中告诉你每条群聊信息的发送者和发送时间，你可以直接称呼发送者为他对应的昵称。
你的回复需要遵守以下几点规则：
- 你可以使用多条消息回复，每两条消息之间使用<botbr>分隔，<botbr>前后不需要包含额外的换行和空格。
- 除<botbr>外，消息中不应该包含其他类似的标记。
- 不要使用markdown或者html，聊天软件不支持解析，换行请用换行符。
- 你应该以普通人的方式发送消息，每条消息字数要尽量少一些，应该倾向于使用更多条的消息回复。
- 代码则不需要分段，用单独的一条消息发送。
- 请使用发送者的昵称称呼发送者，你可以礼貌地问候发送者，但只需要在第一次回答这位发送者的问题时问候他。
- 你有at群成员的能力，只需要在某条消息中插入[CQ:at,qq=（QQ号）]，也就是CQ码。at发送者是非必要的，你可以根据你自己的想法at某个人。
- 你有引用某条消息的能力，使用[CQ:reply,id=（消息id）]来引用。
- 如果有多条消息，你应该优先回复提到你的，一段时间之前的就不要回复了，也可以直接选择不回复。
- 如果你选择完全不回复，你只需要直接输出一个<botbr>。
- 如果你需要思考的话，你应该思考尽量少，以节省时间。
下面是关于你性格的设定，如果设定中提到让你扮演某个人，或者设定中有提到名字，则优先使用设定中的名字。
{state.group_prompt or plugin_config.default_prompt}
"""

            messages: Iterable[ChatCompletionMessageParam] = [
                {"role": "system", "content": systemPrompt}
            ]

            messages += list(state.history)[-plugin_config.history_size :]

            # 没有未处理的消息说明已经被处理了，跳过
            if state.past_events.__len__() < 1:
                break

            # 将机器人错过的消息推送给LLM
            content = ",".join([format_message(ev) for ev in state.past_events])

            logger.debug(
                f"发送API请求 模型：{preset.model_name} 历史消息数：{len(messages)}"
            )
            response = await client.chat.completions.create(
                model=preset.model_name,
                messages=[*messages, {"role": "user", "content": content}],
                max_tokens=preset.max_tokens,
                temperature=preset.temperature,
                timeout=60,
            )

            if response.usage is not None:
                logger.debug(f"收到API响应 使用token数：{response.usage.total_tokens}")

            reply, matched_reasoning_content = pop_reasoning_content(
                response.choices[0].message.content
            )
            reasoning_content: Optional[str] = (
                getattr(response.choices[0].message, "reasoning_content", None)
                or matched_reasoning_content
            )

            # 请求成功后再保存历史记录，保证user和assistant穿插，防止R1模型报错
            state.history.append({"role": "user", "content": content})
            # 添加助手回复到历史
            state.history.append({"role": "assistant", "content": reply})
            state.past_events.clear()

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
            logger.info(
                f"准备发送回复消息 群号：{group_id} 消息分段数：{len(reply.split('<botbr>'))}"
            )
            for r in reply.split("<botbr>"):
                # 似乎会有空消息的情况导致string index out of range异常
                if len(r) == 0 or r.isspace():
                    continue
                # 删除前后多余的换行和空格
                r = r.strip()
                await asyncio.sleep(2)
                logger.debug(
                    f"发送消息分段 内容：{r[:50]}..."
                )  # 只记录前50个字符避免日志过大
                await handler.send(Message(r))

        except Exception as e:
            logger.opt(exception=e).error(f"API请求失败 群号：{group_id}")
            await handler.send(Message(f"服务暂时不可用，请稍后再试\n{e!s}"))
        finally:
            state.queue.task_done()

    state.processing = False


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
    priority=99,
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
    priority=99,
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
    priority=99,
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


# region 持久化与定时任务

# 获取插件数据目录
data_dir = store.get_plugin_data_dir()
# 获取插件数据文件
data_file = store.get_plugin_data_file("llmchat_state.json")


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
                state_data["history"], maxlen=plugin_config.history_size
            )
            state.last_active = state_data["last_active"]
            state.group_prompt = state_data["group_prompt"]
            state.output_reasoning_content = state_data["output_reasoning_content"]
            state.random_trigger_prob = (
                state_data.get("random_trigger_prob")
                or plugin_config.random_trigger_prob
            )
            group_states[int(gid)] = state


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
