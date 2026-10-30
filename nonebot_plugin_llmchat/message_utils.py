import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
import json
import re
import ssl

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
from PIL import Image, ImageOps

from .output_protocol import parse_reply_segments

ChatEvent = GroupMessageEvent | PrivateMessageEvent
MessageSender = Callable[[Message], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class DownloadedImage:
    content: bytes
    mime_type: str
    preview_content: bytes
    preview_mime_type: str


def detect_image_mime(content: bytes, content_type: str | None = None) -> str:
    declared = (content_type or "").split(";", maxsplit=1)[0].strip().lower()
    if declared.startswith("image/"):
        return declared
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def create_image_preview(content: bytes, *, max_edge: int, quality: int) -> tuple[bytes, str]:
    """生成供视觉模型使用的静态压缩预览，原图另行保留。"""
    with Image.open(BytesIO(content)) as source:
        source.seek(0)
        image = ImageOps.exif_transpose(source).convert("RGBA")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        output = BytesIO()
        background.save(output, format="JPEG", quality=quality, optimize=True)
        return output.getvalue(), "image/jpeg"


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


async def download_images(
    event: ChatEvent,
    *,
    preview_max_edge: int = 1024,
    preview_quality: int = 82,
) -> list[DownloadedImage]:
    urls = [segment.data.get("url") or segment.data.get("file") for segment in event.get_message() if segment.type == "image"]
    urls = [url for url in urls if url]
    if not urls:
        return []
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    ssl_context.set_ciphers("DEFAULT@SECLEVEL=2")
    images: list[DownloadedImage] = []
    async with httpx.AsyncClient(verify=ssl_context, timeout=10.0) as client:
        for url in urls:
            try:
                response = await client.get(url)
                response.raise_for_status()
                mime_type = detect_image_mime(response.content, response.headers.get("content-type"))
                try:
                    preview_content, preview_mime_type = await asyncio.to_thread(
                        create_image_preview,
                        response.content,
                        max_edge=preview_max_edge,
                        quality=preview_quality,
                    )
                    logger.debug(f"图片预览压缩完成：{len(response.content)} -> " f"{len(preview_content)} 字节")
                except (OSError, ValueError):
                    logger.exception(f"压缩图片失败，将使用原图: {url}")
                    preview_content, preview_mime_type = response.content, mime_type
                images.append(
                    DownloadedImage(
                        content=response.content,
                        mime_type=mime_type,
                        preview_content=preview_content,
                        preview_mime_type=preview_mime_type,
                    )
                )
            except Exception:
                logger.exception(f"下载图片失败: {url}")
    return images


async def send_reply_messages(sender: MessageSender, content: str) -> None:
    segments = parse_reply_segments(content)
    logger.info(f"准备发送解析后的消息，消息数：{len(segments)}")
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
