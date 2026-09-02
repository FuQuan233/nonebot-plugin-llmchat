# ruff: noqa: I001
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
import unittest

import tests.bootstrap  # noqa: F401
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from nonebot_plugin_llmchat.config import PresetConfig, ScopedConfig
from nonebot_plugin_llmchat.conversation import ConversationService
from nonebot_plugin_llmchat.memory import ConversationTurn, prune_old_turn_images
from nonebot_plugin_llmchat.models import ConversationRecord, ConversationTurnRecord
from nonebot_plugin_llmchat.persistence import StatePersistence
from nonebot_plugin_llmchat.state import StateStore


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(ConversationRecord.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()

    def make_persistence(
        self,
        config: ScopedConfig,
        states: StateStore,
        group_file: Path | None = None,
        private_file: Path | None = None,
    ) -> StatePersistence:
        return StatePersistence(
            config,
            states,
            group_file,
            private_file,
            session_factory=lambda: AsyncSession(self.engine),
        )

    async def test_save_state_normalizes_metadata_and_turns(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x")
        config = ScopedConfig(api_presets=[preset], default_preset="test")
        source = StateStore(config)
        state = source.group_states[1]
        state.turns.append(ConversationTurn([{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]))
        state.memory_summary = "remember"
        persistence = self.make_persistence(config, source)

        await persistence.save_state(1, True, state)
        state.prompt = "new prompt"
        await persistence.save_state(1, True, state)

        async with AsyncSession(self.engine) as session:
            conversation_count = await session.scalar(select(func.count()).select_from(ConversationRecord))
            turn_count = await session.scalar(select(func.count()).select_from(ConversationTurnRecord))
            record = await session.get(ConversationRecord, (1, True))
        assert conversation_count == 1
        assert turn_count == 1
        assert record is not None
        assert record.memory_summary == "remember"
        assert record.prompt == "new prompt"
        assert state.turns[0].sequence == 0

        restored = StateStore(config)
        await self.make_persistence(config, restored).load()
        assert restored.group_states[1].memory_summary == "remember"
        assert restored.group_states[1].prompt == "new prompt"
        assert restored.group_states[1].turns[0].messages[-1]["content"] == "hi"
        assert restored.group_states[1].turns[0].sequence == 0

    async def test_turns_are_inserted_updated_and_deleted_incrementally(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x")
        config = ScopedConfig(api_presets=[preset], default_preset="test")
        states = StateStore(config)
        state = states.group_states[1]
        first = ConversationTurn(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "first"},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,a"}},
                    ],
                }
            ]
        )
        second = ConversationTurn([{"role": "user", "content": "second"}])
        state.turns.extend([first, second])
        persistence = self.make_persistence(config, states)

        await persistence.save_state(1, True, state)
        assert (first.sequence, second.sequence) == (0, 1)
        assert prune_old_turn_images(state.turns, keep_recent_turns=1) == 1
        assert first.dirty
        state.turns.popleft()
        third = ConversationTurn([{"role": "user", "content": "third"}])
        state.turns.append(third)
        await persistence.save_state(1, True, state)

        async with AsyncSession(self.engine) as session:
            rows = list((await session.scalars(select(ConversationTurnRecord).order_by(ConversationTurnRecord.sequence))).all())
            record = await session.get(ConversationRecord, (1, True))
        assert [row.sequence for row in rows] == [1, 2]
        assert [row.messages[0]["content"] for row in rows] == ["second", "third"]
        assert third.sequence == 2
        assert record is not None
        assert record.next_turn_sequence == 3

    async def test_dirty_turn_content_is_updated(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x")
        config = ScopedConfig(api_presets=[preset], default_preset="test")
        states = StateStore(config)
        state = states.group_states[1]
        old = ConversationTurn(
            [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,a"}}],
                }
            ]
        )
        state.turns.extend([old, ConversationTurn([{"role": "user", "content": "latest"}])])
        persistence = self.make_persistence(config, states)
        await persistence.save_state(1, True, state)

        prune_old_turn_images(state.turns, keep_recent_turns=1)
        await persistence.save_state(1, True, state)

        async with AsyncSession(self.engine) as session:
            row = await session.get(ConversationTurnRecord, (1, True, 0))
        assert row is not None
        assert row.messages[0]["content"] == [{"type": "text", "text": "[历史消息包含图片]"}]
        assert not old.dirty

    async def test_conversation_persists_final_state_without_periodic_job(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x")
        config = ScopedConfig(api_presets=[preset], default_preset="test")
        states = StateStore(config)
        state = states.group_states[1]
        event = SimpleNamespace(time=123)
        state.pending_events.append(event)
        saved = []

        async def save_state(context_id, is_group, current_state):
            saved.append((context_id, is_group, current_state.prompt, current_state.last_active))

        async def sender(_message):
            pass

        async def process_snapshot(*_args, **_kwargs):
            state.prompt = "updated"

        service = ConversationService(config, states, {"bot"}, sender, save_state)
        service._process_snapshot = cast(Any, process_snapshot)

        await service.process_event(1, True, state, cast(Any, event))

        assert saved == [(1, True, "updated", 123)]

    async def test_old_json_is_imported_once_and_archived(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x")
        config = ScopedConfig(api_presets=[preset], default_preset="test")
        old_history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
            {"role": "assistant", "content": "done"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            group_file = Path(directory) / "groups.json"
            private_file = Path(directory) / "private.json"
            group_file.write_text(json.dumps({"1": {"preset": "test", "history": old_history}}), encoding="utf8")
            private_file.write_text(json.dumps({"42": {"preset": "test", "prompt": "remember me"}}), encoding="utf8")
            states = StateStore(config)

            await self.make_persistence(config, states, group_file, private_file).load()

            assert len(states.group_states[1].turns) == 1
            assert [message["role"] for message in states.group_states[1].turns[0].messages] == [
                "user",
                "assistant",
                "tool",
                "assistant",
            ]
            assert states.private_states[42].prompt == "remember me"
            assert not group_file.exists()
            assert not private_file.exists()
            assert Path(f"{group_file}.migrated").exists()
            assert Path(f"{private_file}.migrated").exists()

            restored = StateStore(config)
            await self.make_persistence(config, restored, group_file, private_file).load()
            assert restored.group_states[1].turns[0].messages[-1]["content"] == "done"
            assert restored.private_states[42].prompt == "remember me"
