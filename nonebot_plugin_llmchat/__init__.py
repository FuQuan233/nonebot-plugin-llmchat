import aiofiles
from nonebot import get_plugin_config, on_message, logger, on_command, get_driver
from nonebot.plugin import PluginMetadata
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.params import CommandArg
from nonebot.rule import Rule
from nonebot.permission import SUPERUSER
from typing import Dict
from datetime import datetime
from collections import deque
import asyncio
from openai import AsyncOpenAI
from .config import Config, PresetConfig
import time
import json
import os
import random
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import time

__plugin_meta__ = PluginMetadata(
    name="llmchat",
    description="支持多API预设配置的AI群聊插件",
    usage="""@机器人 + 消息 开启对话""",
    type="application",
    homepage="https://github.com/FuQuan233/nonebot-plugin-llmchat",
    config=Config,
    supported_adapters={"~onebot.v11"},
)

pluginConfig = get_plugin_config(Config).llmchat
driver = get_driver()

# 初始化群组状态
class GroupState:
    def __init__(self):
        self.preset_name = pluginConfig.default_preset
        self.history = deque(maxlen=pluginConfig.history_size)
        self.queue = asyncio.Queue()
        self.processing = False
        self.last_active = time.time()
        self.past_events = deque(maxlen=pluginConfig.past_events_size)
        self.group_prompt = None
        self.output_reasoning_content = False

group_states: Dict[int, GroupState] = {}

# 获取当前预设配置
def get_preset(group_id: int) -> PresetConfig:
    state = group_states[group_id]
    for preset in pluginConfig.api_presets:
        if preset.name == state.preset_name:
            return preset
    return pluginConfig.api_presets[0]  # 默认返回第一个预设

# 消息格式转换
def format_message(event: GroupMessageEvent) -> Dict:
    text_message = ""
    if event.reply != None:
        text_message += f"[回复 {event.reply.sender.nickname} 的消息 {event.reply.message.extract_plain_text()}]\n"

    if event.is_tome():
        text_message += f"@{list(driver.config.nickname)[0]} "

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

    message =  {
        "SenderNickname": str(event.sender.card or event.sender.nickname),
        "SenderUserId": str(event.user_id),
        "Message": text_message,
        "SendTime" : datetime.fromtimestamp(event.time).isoformat()
    }
    return json.dumps(message, ensure_ascii=False)

async def isTriggered(event: GroupMessageEvent) -> bool:
    """扩展后的消息处理规则"""
    
    group_id = event.group_id

    if group_id not in group_states:
        logger.info(f"初始化群组状态，群号：{group_id}")
        group_states[group_id] = GroupState()
    
    state = group_states[group_id]

    if state.preset_name == "off":
        return False

    state.past_events.append(event)

    # 原有@触发条件
    if event.is_tome():
        return True
    
    # 随机触发条件
    if random.random() < pluginConfig.random_trigger_prob:
        return True
    
    return False


# 消息处理器
handler = on_message(
    rule=Rule(isTriggered),
    priority=10,
    block=False,
)

@handler.handle()
async def handle_message(event: GroupMessageEvent):
    group_id = event.group_id
    logger.debug(f"收到群聊消息 群号：{group_id} 用户：{event.user_id} 内容：{event.get_plaintext()}")

    if group_id not in group_states:
        group_states[group_id] = GroupState()
    
    state = group_states[group_id]
    
    await state.queue.put(event)
    if not state.processing:
        state.processing = True
        asyncio.create_task(process_messages(group_id))

async def process_messages(group_id: int):
    state = group_states[group_id]
    preset = get_preset(group_id)
    
    # 初始化OpenAI客户端
    client = AsyncOpenAI(
        base_url=preset.api_base,
        api_key=preset.api_key,
        timeout=pluginConfig.request_timeout
    )
    
    logger.info(f"开始处理群聊消息 群号：{group_id} 当前队列长度：{state.queue.qsize()}")
    while not state.queue.empty():
        event = await state.queue.get()
        logger.debug(f"从队列获取消息 群号：{group_id} 消息ID：{event.message_id}")
        try:
            systemPrompt = (
f'''
我想要你帮我在群聊中闲聊，大家一般叫你{"、".join(list(driver.config.nickname))}，我将会在后面的信息中告诉你每条群聊信息的发送者和发送时间，你可以直接称呼发送者为他对应的昵称。
你的回复需要遵守以下几点规则：
- 你可以使用多条消息回复，每两条消息之间使用<botbr>分隔，<botbr>前后不需要包含额外的换行和空格。
- 除<botbr>外，消息中不应该包含其他类似的标记。
- 不要使用markdown格式，聊天软件不支持markdown解析。
- 你应该以普通人的方式发送消息，每条消息字数要尽量少一些，应该倾向于使用更多条的消息回复。
- 代码则不需要分段，用单独的一条消息发送。
- 请使用发送者的昵称称呼发送者，你可以礼貌地问候发送者，但只需要在第一次回答这位发送者的问题时问候他。
- 你有at群成员的能力，只需要在某条消息中插入[CQ:at,qq=（QQ号）]，也就是CQ码。at发送者是非必要的，你可以根据你自己的想法at某个人。
- 如果有多条消息，你应该优先回复提到你的，一段时间之前的就不要回复了，也可以直接选择不回复。
- 如果你需要思考的话，你应该思考尽量少，以节省时间。
下面是关于你性格的设定，如果设定中提到让你扮演某个人，或者设定中有提到名字，则优先使用设定中的名字。
{state.group_prompt or pluginConfig.default_prompt}
'''
            )

            messages = [{"role": "system", "content": systemPrompt}]

            messages += list(state.history)[-pluginConfig.history_size:]

            # 没有未处理的消息说明已经被处理了，跳过
            if state.past_events.__len__() < 1:
                break

            # 将机器人错过的消息推送给LLM
            content = ",".join([format_message(ev) for ev in state.past_events])

            logger.debug(f"发送API请求 模型：{preset.model_name} 历史消息数：{len(messages)}")
            response = await client.chat.completions.create(
                model=preset.model_name,
                messages=messages + [{"role": "user", "content": content}],
                max_tokens=preset.max_tokens,
                temperature=preset.temperature,
                timeout=60
            )
            logger.debug(f"收到API响应 使用token数：{response.usage.total_tokens}")

            reply = response.choices[0].message.content

            # 请求成功后再保存历史记录，保证user和assistant穿插，防止R1模型报错
            state.history.append({"role": "user", "content": content})
            state.past_events.clear()

            reasoning_content: str | None = getattr(response.choices[0].message, "reasoning_content", None)
            if state.output_reasoning_content and reasoning_content:
                await handler.send(Message(reasoning_content))

            logger.info(f"准备发送回复消息 群号：{group_id} 消息分段数：{len(reply.split('<botbr>'))}")
            for r in reply.split("<botbr>"):
                # 删除前后多余的换行和空格
                while r[0] == "\n" or r[0] == " ": r = r[1:]
                while r[-1] == "\n" or r[0] == " ": r = r[:-1]
                await asyncio.sleep(2)
                logger.debug(f"发送消息分段 内容：{r[:50]}...")  # 只记录前50个字符避免日志过大
                await handler.send(Message(r))
            
            # 添加助手回复到历史
            state.history.append({
                "role": "assistant",
                "content": reply,
            })
            
        except Exception as e:
            logger.error(f"API请求失败 群号：{group_id} 错误：{str(e)}", exc_info=True)
            await handler.send(Message(f"服务暂时不可用，请稍后再试\n{str(e)}"))
        finally:
            state.queue.task_done()
    
    state.processing = False

# 预设切换命令
preset_handler = on_command("API预设", priority=1, block=True, permission=SUPERUSER)
@preset_handler.handle()
async def handle_preset(event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id
    preset_name = args.extract_plain_text().strip()
    
    if group_id not in group_states:
        group_states[group_id] = GroupState()
    
    if preset_name == "off":
        group_states[group_id].preset_name = preset_name
        await preset_handler.finish(f"已关闭llmchat")

    available_presets = {p.name for p in pluginConfig.api_presets}
    if preset_name not in available_presets:
        await preset_handler.finish(f"当前API预设：{group_states[group_id].preset_name}\n可用API预设：\n- {'\n- '.join(available_presets)}")
    
    group_states[group_id].preset_name = preset_name
    await preset_handler.finish(f"已切换至API预设：{preset_name}")

preset_handler = on_command("修改设定", priority=1, block=True, permission=(SUPERUSER|GROUP_ADMIN|GROUP_OWNER))
@preset_handler.handle()
async def handle_preset(event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id
    group_prompt = args.extract_plain_text().strip()
    
    if group_id not in group_states:
        group_states[group_id] = GroupState()
    
    group_states[group_id].group_prompt = group_prompt
    await preset_handler.finish("修改成功")

reset_handler = on_command("记忆清除", priority=99, block=True, permission=(SUPERUSER|GROUP_ADMIN|GROUP_OWNER))
@reset_handler.handle()
async def handle_reset(event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id

    if group_id not in group_states:
        group_states[group_id] = GroupState()

    group_states[group_id].past_events.clear()
    group_states[group_id].history.clear()
    await preset_handler.finish(f"记忆已清空")

# 预设切换命令
preset_handler = on_command("切换思维输出", priority=1, block=True, permission=(SUPERUSER|GROUP_ADMIN|GROUP_OWNER))
@preset_handler.handle()
async def handle_preset(event: GroupMessageEvent, args: Message = CommandArg()):
    group_id = event.group_id
    
    if group_id not in group_states:
        group_states[group_id] = GroupState()
    
    if group_states[group_id].output_reasoning_content:
        group_states[group_id].output_reasoning_content = False
        await preset_handler.finish("已关闭思维输出")
    else:
        group_states[group_id].output_reasoning_content = True
        await preset_handler.finish("已开启思维输出")


# region 持久化与定时任务
async def save_state():
    """保存群组状态到文件"""
    logger.info(f"开始保存群组状态到文件：{pluginConfig.storage_path}")
    data = {
        gid: {
            "preset": state.preset_name,
            "history": list(state.history),
            "last_active": state.last_active,
            "group_prompt": state.group_prompt,
            "output_reasoning_content": state.output_reasoning_content
        }
        for gid, state in group_states.items()
    }
    
    os.makedirs(os.path.dirname(pluginConfig.storage_path), exist_ok=True)
    async with aiofiles.open(pluginConfig.storage_path, "w") as f:
        await f.write(json.dumps(data, ensure_ascii=False))

async def load_state():
    """从文件加载群组状态"""
    logger.info(f"从文件加载群组状态：{pluginConfig.storage_path}")
    if not os.path.exists(pluginConfig.storage_path):
        return
    
    async with aiofiles.open(pluginConfig.storage_path, "r") as f:
        data = json.loads(await f.read())
        for gid, state_data in data.items():
            state = GroupState()
            state.preset_name = state_data["preset"]
            state.history = deque(state_data["history"], maxlen=pluginConfig.history_size)
            state.last_active = state_data["last_active"]
            state.group_prompt = state_data["group_prompt"]
            state.output_reasoning_content = state_data["output_reasoning_content"]
            group_states[int(gid)] = state

# 注册生命周期事件
@driver.on_startup
async def init_plugin():
    logger.info("插件启动初始化")
    await load_state()
    scheduler = AsyncIOScheduler()
    # 每5分钟保存状态
    scheduler.add_job(save_state, 'interval', minutes=5)
    scheduler.start()

@driver.on_shutdown
async def cleanup_plugin():
    logger.info("插件关闭清理")
    await save_state()
