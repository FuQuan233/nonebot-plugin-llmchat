import random

from nonebot import get_driver, get_plugin_config, logger, on_message, require
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

require("nonebot_plugin_datastore")
from nonebot_plugin_datastore.db import post_db_init

from .commands import register_commands
from .config import Config
from .conversation import ConversationService
from .dispatcher import MessageDispatcher
from .mcpclient import MCPClient
from .models import plugin_data
from .persistence import StatePersistence
from .state import StateStore
from .stickers import StickerService

__plugin_meta__ = PluginMetadata(
    name="llmchat",
    description="支持多API预设、MCP协议、联网搜索、视觉模型的AI群聊插件",
    usage="@机器人 + 消息 开启对话",
    type="application",
    homepage="https://github.com/FuQuan233/nonebot-plugin-llmchat",
    config=Config,
    supported_adapters={"~onebot.v11"},
)

plugin_config = get_plugin_config(Config).llmchat
driver = get_driver()
states = StateStore(plugin_config)
state_persistence = StatePersistence(
    plugin_config,
    states,
    plugin_data.data_dir / "llmchat_state.json",
    plugin_data.data_dir / "llmchat_private_state.json",
)
sticker_service = StickerService(
    plugin_data.data_dir / "stickers",
    plugin_config.sticker_max_file_size,
)

# 保留旧版公开名称，避免依赖插件内部状态的代码立即失效。
group_states = states.group_states
private_chat_states = states.private_states


async def is_triggered(event: GroupMessageEvent | PrivateMessageEvent) -> bool:
    is_group = isinstance(event, GroupMessageEvent)
    if not is_group and not plugin_config.enable_private_chat:
        return False
    state = states.get(event.group_id if is_group else event.user_id, is_group)
    if state.preset_name == "off" or event.user_id in plugin_config.blacklist_user_ids:
        return False
    text = event.get_plaintext().strip()
    if any(text.startswith(prefix) for prefix in plugin_config.ignore_prefixes):
        return False

    state.pending_events.append(event)
    if not is_group:
        return True
    return event.is_tome() or random.random() < state.random_trigger_prob


handler = on_message(rule=Rule(is_triggered), priority=99, block=False)
conversation = ConversationService(
    config=plugin_config,
    states=states,
    bot_names=set(driver.config.nickname),
    sender=handler.send,
    save_state=state_persistence.save_state,
    sticker_service=sticker_service,
)
message_dispatcher = MessageDispatcher(conversation.process_event)


@handler.handle()
async def handle_message(event: GroupMessageEvent | PrivateMessageEvent) -> None:
    is_group = isinstance(event, GroupMessageEvent)
    context_id = event.group_id if is_group else event.user_id
    state = states.get(context_id, is_group)
    logger.debug(
        f"收到{'群聊' if is_group else '私聊'}消息 " f"会话：{context_id} 用户：{event.user_id} 内容：{event.get_plaintext()}"
    )
    await message_dispatcher.enqueue(context_id, is_group, state, event)


register_commands(plugin_config, states, state_persistence.save_state)


@post_db_init
async def init_plugin() -> None:
    logger.info("llmchat插件启动初始化")
    await state_persistence.load()


@driver.on_shutdown
async def cleanup_plugin() -> None:
    logger.info("llmchat插件关闭清理")
    await message_dispatcher.shutdown()
    await MCPClient.destroy_instance()
