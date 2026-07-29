from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER, PRIVATE
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from .config import ScopedConfig
from .state import ChatState, StateStore


def _event_state(config: ScopedConfig, states: StateStore, event) -> ChatState | None:
    if isinstance(event, GroupMessageEvent):
        return states.group_states[event.group_id]
    if config.enable_private_chat:
        return states.private_states[event.user_id]
    return None


def register_commands(config: ScopedConfig, states: StateStore) -> None:
    preset_handler = on_command("API预设", priority=1, block=True, permission=SUPERUSER)

    @preset_handler.handle()
    async def handle_preset(event: GroupMessageEvent | PrivateMessageEvent, args: Message = CommandArg()):
        text = args.extract_plain_text().strip()
        parts = text.split(maxsplit=1)
        target_id: int | None = None
        is_group_target = isinstance(event, GroupMessageEvent)
        state = _event_state(config, states, event)
        if state is None:
            return
        preset_name = text
        if isinstance(event, PrivateMessageEvent) and parts and parts[0].isdigit():
            target_id = int(parts[0])
            is_group_target = target_id in states.group_states
            state = states.get(target_id, is_group_target)
            preset_name = parts[1] if len(parts) > 1 else ""

        available = {preset.name for preset in config.api_presets}
        if not preset_name or preset_name not in available | {"off"}:
            names = "\n- ".join(sorted(available))
            await preset_handler.finish(f"当前API预设：{state.preset_name}\n可用API预设：\n- {names}")
        state.preset_name = preset_name
        if target_id is not None:
            kind = "群聊" if is_group_target else "私聊"
            await preset_handler.finish(f"已将 {kind} {target_id} 切换至API预设：{preset_name}")
        await preset_handler.finish(f"已切换至API预设：{preset_name}")

    edit_handler = on_command(
        "修改设定",
        priority=1,
        block=True,
        permission=SUPERUSER | GROUP_ADMIN | GROUP_OWNER | PRIVATE,
    )

    @edit_handler.handle()
    async def handle_edit(event: GroupMessageEvent | PrivateMessageEvent, args: Message = CommandArg()):
        state = _event_state(config, states, event)
        if state is None:
            return
        state.prompt = args.extract_plain_text().strip()
        await edit_handler.finish("修改成功")

    reset_handler = on_command(
        "记忆清除",
        priority=1,
        block=True,
        permission=SUPERUSER | GROUP_ADMIN | GROUP_OWNER | PRIVATE,
    )

    @reset_handler.handle()
    async def handle_reset(event: GroupMessageEvent | PrivateMessageEvent):
        state = _event_state(config, states, event)
        if state is None:
            return
        state.pending_events.clear()
        state.history.clear()
        await reset_handler.finish("记忆已清空")

    probability_handler = on_command(
        "设置主动回复概率",
        priority=1,
        block=True,
        permission=SUPERUSER | GROUP_ADMIN | GROUP_OWNER,
    )

    @probability_handler.handle()
    async def handle_probability(event: GroupMessageEvent, args: Message = CommandArg()):
        try:
            probability = float(args.extract_plain_text().strip())
            if not 0 <= probability <= 1:
                raise ValueError("概率必须在0到1之间")
        except ValueError as error:
            await probability_handler.finish(f"输入有误，请使用 [0,1] 的浮点数\n{error!s}")
            return
        states.group_states[event.group_id].random_trigger_prob = probability
        await probability_handler.finish(f"主动回复概率已设为 {probability}")

    think_handler = on_command(
        "切换思维输出",
        priority=1,
        block=True,
        permission=SUPERUSER | GROUP_ADMIN | GROUP_OWNER | PRIVATE,
    )

    @think_handler.handle()
    async def handle_think(event: GroupMessageEvent | PrivateMessageEvent):
        state = _event_state(config, states, event)
        if state is None:
            return
        state.output_reasoning_content = not state.output_reasoning_content
        status = "开启" if state.output_reasoning_content else "关闭"
        await think_handler.finish(f"已{status}思维输出")
