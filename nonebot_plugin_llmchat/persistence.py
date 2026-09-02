import asyncio
from collections import deque
from collections.abc import Callable
import json
from pathlib import Path
from typing import Any

from nonebot import logger
from nonebot_plugin_datastore import create_session
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import ScopedConfig
from .memory import ConversationTurn, restore_turns
from .models import ConversationRecord, ConversationTurnRecord
from .state import ChatState, StateStore

SessionFactory = Callable[[], AsyncSession]


class StatePersistence:
    """使用 nonebot-plugin-datastore 实时、按轮次增量持久化会话状态。"""

    def __init__(
        self,
        config: ScopedConfig,
        states: StateStore,
        legacy_group_file: Path | None = None,
        legacy_private_file: Path | None = None,
        *,
        session_factory: SessionFactory = create_session,
    ) -> None:
        self.config = config
        self.states = states
        self.legacy_group_file = legacy_group_file
        self.legacy_private_file = legacy_private_file
        self._session_factory = session_factory
        self._lock = asyncio.Lock()

    def _restore_legacy(self, raw: dict[str, Any], *, is_group: bool) -> ChatState:
        state = ChatState(
            preset_name=raw.get(
                "preset",
                self.config.default_preset if is_group else self.config.private_chat_preset,
            ),
            past_events_size=self.config.past_events_size,
            random_trigger_prob=raw.get("random_trigger_prob", self.config.random_trigger_prob) if is_group else 0.0,
        )
        if raw.get("memory_version") == 2:
            state.turns = deque(ConversationTurn(messages) for messages in raw.get("turns", []) if isinstance(messages, list))
            state.memory_summary = raw.get("memory_summary")
            state.last_prompt_tokens = raw.get("last_prompt_tokens")
            state.last_cached_tokens = raw.get("last_cached_tokens")
            state.usage_preset_name = raw.get("usage_preset_name")
        else:
            state.turns = deque(restore_turns(raw.get("history", [])))
        state.last_active = raw.get("last_active", state.last_active)
        state.prompt = raw.get("group_prompt", raw.get("prompt"))
        state.output_reasoning_content = raw.get("output_reasoning_content", False)
        return state

    def _state_from_record(
        self,
        record: ConversationRecord,
        turns: list[ConversationTurnRecord],
    ) -> ChatState:
        state = ChatState(
            preset_name=record.preset_name,
            past_events_size=self.config.past_events_size,
            random_trigger_prob=(record.random_trigger_prob or 0.0) if record.is_group else 0.0,
        )
        state.turns = deque(ConversationTurn(messages=turn.messages, sequence=turn.sequence) for turn in turns)
        state.memory_summary = record.memory_summary
        state.last_prompt_tokens = record.last_prompt_tokens
        state.last_cached_tokens = record.last_cached_tokens
        state.usage_preset_name = record.usage_preset_name
        state.last_active = record.last_active
        state.prompt = record.prompt
        state.output_reasoning_content = record.output_reasoning_content
        return state

    @staticmethod
    def _update_record(record: ConversationRecord, state: ChatState, is_group: bool) -> None:
        record.preset_name = state.preset_name
        record.memory_summary = state.memory_summary
        record.last_prompt_tokens = state.last_prompt_tokens
        record.last_cached_tokens = state.last_cached_tokens
        record.usage_preset_name = state.usage_preset_name
        record.last_active = state.last_active
        record.prompt = state.prompt
        record.output_reasoning_content = state.output_reasoning_content
        record.random_trigger_prob = state.random_trigger_prob if is_group else None

    @staticmethod
    async def _read_legacy_json(path: Path | None) -> dict[str, dict[str, Any]] | None:
        if path is None or not path.exists():
            return None
        try:
            content = await asyncio.to_thread(path.read_text, encoding="utf8")
            value = json.loads(content)
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            logger.exception(f"读取llmchat旧版状态失败: {path}")
            return None

    @staticmethod
    def _archive_legacy_file(path: Path) -> Path:
        archive = path.with_name(f"{path.name}.migrated")
        index = 1
        while archive.exists():
            archive = path.with_name(f"{path.name}.migrated.{index}")
            index += 1
        path.replace(archive)
        return archive

    async def _add_state(
        self,
        session: AsyncSession,
        context_id: int,
        is_group: bool,
        state: ChatState,
    ) -> None:
        record = ConversationRecord(
            context_id=context_id,
            is_group=is_group,
            preset_name=state.preset_name,
            last_active=state.last_active,
            output_reasoning_content=state.output_reasoning_content,
            next_turn_sequence=len(state.turns),
        )
        self._update_record(record, state, is_group)
        session.add(record)
        for sequence, turn in enumerate(state.turns):
            turn.sequence = sequence
            session.add(
                ConversationTurnRecord(
                    context_id=context_id,
                    is_group=is_group,
                    sequence=sequence,
                    messages=turn.messages,
                )
            )

    async def _import_legacy_json(
        self,
        session: AsyncSession,
        known_keys: set[tuple[int, bool]],
    ) -> tuple[int, list[Path]]:
        imported = 0
        migrated_files: list[Path] = []
        for path, is_group in (
            (self.legacy_group_file, True),
            (self.legacy_private_file, False),
        ):
            raw_states = await self._read_legacy_json(path)
            if raw_states is None or path is None:
                continue
            for raw_context_id, raw in raw_states.items():
                if not isinstance(raw, dict):
                    continue
                try:
                    context_id = int(raw_context_id)
                except (TypeError, ValueError):
                    logger.warning(f"忽略无法识别的旧版会话ID: {raw_context_id!r}")
                    continue
                key = (context_id, is_group)
                if key in known_keys:
                    continue
                state = self._restore_legacy(raw, is_group=is_group)
                target = self.states.group_states if is_group else self.states.private_states
                target[context_id] = state
                await self._add_state(session, context_id, is_group, state)
                known_keys.add(key)
                imported += 1
            migrated_files.append(path)
        return imported, migrated_files

    async def load(self) -> None:
        async with self._lock, self._session_factory() as session:
            records = list((await session.scalars(select(ConversationRecord))).all())
            turn_rows = list(
                (
                    await session.scalars(
                        select(ConversationTurnRecord).order_by(
                            ConversationTurnRecord.context_id,
                            ConversationTurnRecord.is_group,
                            ConversationTurnRecord.sequence,
                        )
                    )
                ).all()
            )
            turns_by_key: dict[tuple[int, bool], list[ConversationTurnRecord]] = {}
            for turn in turn_rows:
                turns_by_key.setdefault((turn.context_id, turn.is_group), []).append(turn)

            known_keys: set[tuple[int, bool]] = set()
            for record in records:
                key = (record.context_id, record.is_group)
                state = self._state_from_record(record, turns_by_key.get(key, []))
                target = self.states.group_states if record.is_group else self.states.private_states
                target[record.context_id] = state
                known_keys.add(key)

            imported, migrated_files = await self._import_legacy_json(session, known_keys)
            if imported:
                await session.commit()

        for path in migrated_files:
            archive = await asyncio.to_thread(self._archive_legacy_file, path)
            logger.info(f"已将旧版会话数据迁移至数据库，原文件保留为: {archive}")
        logger.info(f"已从数据库加载{len(known_keys)}个llmchat会话，迁移旧版会话{imported}个")

    async def save_state(self, context_id: int, is_group: bool, state: ChatState) -> None:
        assignments: list[tuple[ConversationTurn, int]] = []
        dirty_turns: list[ConversationTurn] = []
        async with self._lock, self._session_factory() as session:
            record = await session.get(ConversationRecord, (context_id, is_group))
            if record is None:
                record = ConversationRecord(
                    context_id=context_id,
                    is_group=is_group,
                    preset_name=state.preset_name,
                    last_active=state.last_active,
                    output_reasoning_content=state.output_reasoning_content,
                    next_turn_sequence=0,
                )
                session.add(record)
            self._update_record(record, state, is_group)

            stored_sequences = set(
                (
                    await session.scalars(
                        select(ConversationTurnRecord.sequence).where(
                            ConversationTurnRecord.context_id == context_id,
                            ConversationTurnRecord.is_group == is_group,
                        )
                    )
                ).all()
            )
            active_sequences: set[int] = set()
            for turn in state.turns:
                if turn.sequence is None:
                    sequence = record.next_turn_sequence
                    record.next_turn_sequence += 1
                    session.add(
                        ConversationTurnRecord(
                            context_id=context_id,
                            is_group=is_group,
                            sequence=sequence,
                            messages=turn.messages,
                        )
                    )
                    assignments.append((turn, sequence))
                else:
                    sequence = turn.sequence
                    if sequence not in stored_sequences:
                        session.add(
                            ConversationTurnRecord(
                                context_id=context_id,
                                is_group=is_group,
                                sequence=sequence,
                                messages=turn.messages,
                            )
                        )
                    elif turn.dirty:
                        row = await session.get(ConversationTurnRecord, (context_id, is_group, sequence))
                        if row is not None:
                            row.messages = turn.messages
                            dirty_turns.append(turn)
                active_sequences.add(sequence)

            removed_sequences = stored_sequences - active_sequences
            if removed_sequences:
                await session.execute(
                    delete(ConversationTurnRecord).where(
                        ConversationTurnRecord.context_id == context_id,
                        ConversationTurnRecord.is_group == is_group,
                        ConversationTurnRecord.sequence.in_(removed_sequences),
                    )
                )
            await session.commit()
            for turn, sequence in assignments:
                turn.sequence = sequence
                turn.dirty = False
            for turn in dirty_turns:
                turn.dirty = False
