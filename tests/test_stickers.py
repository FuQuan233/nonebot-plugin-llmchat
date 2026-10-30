# ruff: noqa: I001
from pathlib import Path
import tempfile
import unittest

import tests.bootstrap  # noqa: F401
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from nonebot_plugin_llmchat.models import StickerRecord
from nonebot_plugin_llmchat.stickers import StickerCandidate, StickerService


class StickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with self.engine.begin() as connection:
            await connection.run_sync(StickerRecord.metadata.create_all)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = StickerService(
            Path(self.temp_dir.name),
            1024,
            session_factory=lambda: AsyncSession(self.engine),
        )

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp_dir.cleanup()

    @staticmethod
    def candidate(candidate_id: str, content: bytes, context_id: int) -> StickerCandidate:
        return StickerCandidate(
            id=candidate_id,
            content=content,
            mime_type="image/png",
            context_id=context_id,
            is_group=True,
            message_id=context_id + 100,
            user_id=context_id + 200,
        )

    async def test_collects_all_candidates_and_deduplicates_globally(self):
        candidates = {
            "first": self.candidate("first", b"first-image", 1),
            "second": self.candidate("second", b"second-image", 2),
        }
        for candidate_id in candidates:
            result = await self.service.collect(
                {
                    "candidate_id": candidate_id,
                    "description": f"表情 {candidate_id}",
                    "emotion": "开心",
                    "keywords": ["高兴", "猫"],
                },
                candidates,
            )
            assert "收藏成功" in result

        duplicate = await self.service.collect(
            {
                "candidate_id": "first",
                "description": "重复",
                "emotion": "开心",
                "keywords": [],
            },
            candidates,
        )
        async with AsyncSession(self.engine) as session:
            count = await session.scalar(select(func.count()).select_from(StickerRecord))
        assert count == 2
        assert "已经收藏过" in duplicate
        assert len(list(Path(self.temp_dir.name).iterdir())) == 2

    async def test_send_immediately_uses_global_library_and_reports_match(self):
        candidate = self.candidate("global", b"global-image", 123)
        await self.service.collect(
            {
                "candidate_id": "global",
                "description": "一只猫开心鼓掌",
                "emotion": "开心",
                "keywords": ["鼓掌", "猫"],
            },
            {"global": candidate},
        )
        sent = []

        async def sender(message):
            sent.append(message)

        result = await self.service.send(
            {"emotion": "开心", "keywords": ["鼓掌"], "description": "开心地鼓掌"},
            sender,
        )

        assert "已找到并发送" in result
        assert len(sent) == 1
        assert sent[0][0].type == "image"
        async with AsyncSession(self.engine) as session:
            sticker = await session.scalar(select(StickerRecord))
        assert sticker is not None
        assert sticker.use_count == 1
        assert sticker.last_used_at is not None

    async def test_send_explicitly_reports_no_match(self):
        sent = []

        async def sender(message):
            sent.append(message)

        result = await self.service.send(
            {"emotion": "疑惑", "keywords": ["问号"], "description": "疑惑"},
            sender,
        )

        assert result == "未找到匹配的表情包，本次没有发送图片"
        assert sent == []
