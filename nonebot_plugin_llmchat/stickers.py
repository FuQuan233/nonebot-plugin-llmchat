import asyncio
from dataclasses import dataclass
import hashlib
from pathlib import Path
import random
import time
from typing import Any
from uuid import uuid4

from nonebot import logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot_plugin_datastore import create_session
from sqlalchemy import select

from .message_utils import MessageSender
from .models import StickerRecord
from .persistence import SessionFactory

COLLECT_TOOL_NAME = "sticker__collect"
SEND_TOOL_NAME = "sticker__send"


@dataclass(frozen=True, slots=True)
class StickerCandidate:
    id: str
    content: bytes
    mime_type: str
    context_id: int
    is_group: bool
    message_id: int
    user_id: int


class StickerService:
    """管理当前请求的图片候选与全局共享表情包库。"""

    def __init__(
        self,
        data_dir: Path,
        max_file_size: int,
        *,
        session_factory: SessionFactory = create_session,
    ) -> None:
        self.data_dir = data_dir
        self.max_file_size = max_file_size
        self._session_factory = session_factory
        self._lock = asyncio.Lock()

    @staticmethod
    def get_tools() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": COLLECT_TOOL_NAME,
                    "description": (
                        "将当前消息里值得反复使用的反应图、梗图或表情包收藏到全局共享表情库。"
                        "看到好用的表情包时应积极收藏；可以连续收藏多张。不要收藏普通照片、截图、二维码或隐私内容。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "candidate_id": {"type": "string", "description": "图片前标注的候选ID"},
                            "description": {"type": "string", "description": "准确描述画面、动作和适用语境"},
                            "emotion": {"type": "string", "description": "最主要的情绪或反应，例如开心、嫌弃、无语"},
                            "keywords": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "便于以后匹配的简短关键词",
                            },
                            "recognized_text": {"type": "string", "description": "图中文字，没有则留空"},
                        },
                        "required": ["candidate_id", "description", "emotion", "keywords"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": SEND_TOOL_NAME,
                    "description": (
                        "从全局共享表情库匹配并立即发送一张表情包。聊天中适合用表情回应时应积极调用；"
                        "每次调用都会独立匹配并发送，可以按你的判断连续调用。工具会明确返回是否找到并成功发送。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "emotion": {"type": "string", "description": "希望表达的主要情绪或反应"},
                            "keywords": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "希望匹配的画面、语境或梗关键词",
                            },
                            "description": {"type": "string", "description": "希望发送的表情包语义"},
                        },
                        "required": ["emotion", "keywords", "description"],
                    },
                },
            },
        ]

    @staticmethod
    def is_sticker_tool(name: str) -> bool:
        return name in {COLLECT_TOOL_NAME, SEND_TOOL_NAME}

    @staticmethod
    def _extension(mime_type: str) -> str:
        return {
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/jpeg": ".jpg",
        }.get(mime_type, ".img")

    @staticmethod
    def _keywords(value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("keywords必须是字符串数组")
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        candidates: dict[str, StickerCandidate],
        sender: MessageSender,
    ) -> str:
        try:
            if name == COLLECT_TOOL_NAME:
                return await self.collect(arguments, candidates)
            if name == SEND_TOOL_NAME:
                return await self.send(arguments, sender)
            return f"未知的表情包工具: {name}"
        except (OSError, ValueError, TypeError) as error:
            logger.opt(exception=error).error(f"执行表情包工具 {name} 失败")
            return f"表情包工具执行失败：{error!s}"
        except Exception as error:
            logger.opt(exception=error).error(f"执行表情包工具 {name} 失败")
            return "表情包工具执行失败，未修改或发送表情包"

    async def collect(
        self,
        arguments: dict[str, Any],
        candidates: dict[str, StickerCandidate],
    ) -> str:
        candidate_id = str(arguments.get("candidate_id", ""))
        candidate = candidates.get(candidate_id)
        if candidate is None:
            return f"收藏失败：当前轮次不存在图片候选 {candidate_id!r}"
        if len(candidate.content) > self.max_file_size:
            return f"收藏失败：图片大小超过 {self.max_file_size} 字节"

        description = str(arguments.get("description", "")).strip()
        emotion = str(arguments.get("emotion", "")).strip()
        keywords = self._keywords(arguments.get("keywords"))
        recognized_text = str(arguments.get("recognized_text", "")).strip() or None
        if not description or not emotion:
            return "收藏失败：description和emotion不能为空"

        digest = hashlib.sha256(candidate.content).hexdigest()
        file_name = f"{digest}{self._extension(candidate.mime_type)}"
        async with self._lock, self._session_factory() as session:
            existing = await session.scalar(select(StickerRecord).where(StickerRecord.sha256 == digest))
            if existing is not None:
                return f"该表情包已经收藏过：id={existing.id}，描述={existing.description}"

            await asyncio.to_thread(self._write_file, file_name, candidate.content)
            sticker_id = str(uuid4())
            sticker = StickerRecord(
                id=sticker_id,
                sha256=digest,
                file_name=file_name,
                mime_type=candidate.mime_type,
                file_size=len(candidate.content),
                description=description,
                emotion=emotion,
                keywords=keywords,
                recognized_text=recognized_text,
                source_context_id=candidate.context_id,
                source_is_group=candidate.is_group,
                source_message_id=candidate.message_id,
                source_user_id=candidate.user_id,
                created_at=time.time(),
                last_used_at=None,
                use_count=0,
                enabled=True,
            )
            session.add(sticker)
            await session.commit()
        logger.info(f"已收藏表情包 {sticker_id}: {description}")
        return f"收藏成功：id={sticker_id}，描述={description}"

    def _write_file(self, file_name: str, content: bytes) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        target = self.data_dir / file_name
        if not target.exists():
            target.write_bytes(content)

    @classmethod
    def _score(cls, sticker: StickerRecord, arguments: dict[str, Any]) -> int:
        emotion = str(arguments.get("emotion", "")).strip().casefold()
        keywords = cls._keywords(arguments.get("keywords"))
        description = str(arguments.get("description", "")).strip().casefold()
        stored_emotion = sticker.emotion.casefold()
        stored_keywords = [item.casefold() for item in sticker.keywords]
        searchable = " ".join([sticker.description, sticker.emotion, sticker.recognized_text or "", *sticker.keywords]).casefold()
        score = 6 if emotion and emotion == stored_emotion else 0
        if emotion and emotion in searchable:
            score += 2
        for keyword in keywords:
            normalized = keyword.casefold()
            if normalized in stored_keywords:
                score += 4
            elif normalized in searchable:
                score += 2
        if description and (description in searchable or searchable in description):
            score += 2
        return score

    async def send(self, arguments: dict[str, Any], sender: MessageSender) -> str:
        self._keywords(arguments.get("keywords"))
        async with self._lock, self._session_factory() as session:
            stickers = list((await session.scalars(select(StickerRecord).where(StickerRecord.enabled))).all())
            ranked = [(self._score(sticker, arguments), random.random(), sticker) for sticker in stickers]
            ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
            matched = [sticker for score, _, sticker in ranked if score > 0]
            if not matched:
                return "未找到匹配的表情包，本次没有发送图片"

            for sticker in matched:
                path = self.data_dir / sticker.file_name
                try:
                    content = await asyncio.to_thread(path.read_bytes)
                except OSError:
                    sticker.enabled = False
                    logger.warning(f"表情包文件缺失，已禁用记录 {sticker.id}: {path}")
                    continue
                await sender(Message(MessageSegment.image(content)))
                sticker_id = sticker.id
                sticker_description = sticker.description
                sticker.last_used_at = time.time()
                sticker.use_count += 1
                await session.commit()
                logger.info(f"已发送表情包 {sticker_id}: {sticker_description}")
                return f"已找到并发送表情包：id={sticker_id}，描述={sticker_description}"

            await session.commit()
            return "找到了语义匹配记录，但图片文件均不可用，本次没有发送图片"
