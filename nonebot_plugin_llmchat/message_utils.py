import asyncio
import base64
from collections.abc import Awaitable, Callable
from datetime import datetime
import json
import re
import ssl

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent

ChatEvent = GroupMessageEvent | PrivateMessageEvent
MessageSender = Callable[[Message], Awaitable[object]]


def pop_reasoning_content(content: str | None) -> tuple[str | None, str | None]:
    if content is None:
        return None, None
    if matched := re.match(r"<think>(.*?)</think>", content, flags=re.DOTALL):
        return content.replace(matched.group(0), "").strip(), matched.group(1).strip()
    return content, None


def format_message(event: ChatEvent, bot_name: str) -> str:
    text = ""
    if isinstance(event, GroupMessageEvent) and event.reply is not None:
        text += f"[回复 {event.reply.sender.nickname} 的消息 {event.reply.message.extract_plain_text()}]\n"
    if isinstance(event, GroupMessageEvent) and event.is_tome():
        text += f"@{bot_name} "
    for segment in event.get_message():
        if segment.type == "at":
            text += segment.data.get("name", "")
        elif segment.type == "image":
            text += "[图片]"
        elif segment.type == "voice":
            text += "[语音]"
        elif segment.type == "text":
            text += segment.data.get("text", "")
    nickname = event.sender.card or event.sender.nickname if isinstance(event, GroupMessageEvent) else event.sender.nickname
    return json.dumps(
        {
            "SenderNickname": str(nickname),
            "SenderUserId": str(event.user_id),
            "Message": text,
            "MessageID": event.message_id,
            "SendTime": datetime.fromtimestamp(event.time).isoformat(),
        },
        ensure_ascii=False,
    )


async def download_images(event: ChatEvent) -> list[str]:
    urls = [segment.data.get("url") or segment.data.get("file") for segment in event.get_message() if segment.type == "image"]
    urls = [url for url in urls if url]
    if not urls:
        return []
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    ssl_context.set_ciphers("DEFAULT@SECLEVEL=2")
    images: list[str] = []
    async with httpx.AsyncClient(verify=ssl_context, timeout=10.0) as client:
        for url in urls:
            try:
                response = await client.get(url)
                response.raise_for_status()
                images.append(base64.b64encode(response.content).decode())
            except Exception:
                logger.exception(f"下载图片失败: {url}")
    return images


async def send_split_messages(sender: MessageSender, content: str) -> None:
    segments = [segment.strip() for segment in content.split("<botbr>") if segment.strip()]
    logger.info(f"准备发送分段消息，分段数：{len(segments)}")
    for index, segment in enumerate(segments):
        if index:
            await asyncio.sleep(2)
        await sender(Message(segment))


def build_reasoning_forward_nodes(self_id: str, nickname: str, content: str) -> list[dict]:
    return [
        {
            "type": "node",
            "data": {"nickname": nickname, "user_id": self_id, "content": f"{nickname}的内心OS:"},
        },
        {
            "type": "node",
            "data": {"nickname": nickname, "user_id": self_id, "content": content},
        },
    ]
