from .config import ScopedConfig
from .state import ChatState


def build_system_prompt(
    *,
    config: ScopedConfig,
    state: ChatState,
    bot_names: set[str],
    is_group: bool,
    support_tools: bool,
) -> str:
    chat_type = "群聊" if is_group else "私聊"
    names = "、".join(sorted(bot_names))
    lines = [
        f"我想要你帮我在{chat_type}中闲聊，大家一般叫你{names}。",
        "我会在后面的信息中告诉你每条信息的发送者和发送时间，你可以直接称呼发送者的昵称。",
        "你的回复需要遵守以下规则：",
        "- 多条消息之间使用<botbr>分隔，标记前后不需要额外换行或空格。",
        "- 除<botbr>外，不要输出其他类似标记。",
        "- 不要使用Markdown或HTML，换行请直接使用换行符。",
        "- 以普通人的方式聊天，每条消息尽量简短；代码应放在单独一条消息中。",
        "- 只在第一次回答某位发送者时礼貌问候。",
        "- 使用[CQ:reply,id=消息id]引用消息。",
        "- 优先回复提到你的新消息；也可以选择不回复。",
        "- 完全不回复时只输出<botbr>。",
        "- 尽量减少不必要的思考。",
    ]
    if is_group:
        lines.append("- 使用[CQ:at,qq=QQ号]提及群成员，是否提及由你决定。")
    lines.extend(
        [
            "下面是你的性格设定；若其中指定了身份或名字，应优先遵守：",
            state.prompt or config.default_prompt,
        ]
    )
    if support_tools:
        additions = [
            f"{name}：{server.additional_prompt}" for name, server in config.mcp_servers.items() if server.additional_prompt
        ]
        if additions:
            lines.extend(["你可以使用工具，额外说明如下：", *additions])
    return "\n".join(lines)
