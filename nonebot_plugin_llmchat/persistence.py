import asyncio
from collections import deque
import json
import os
from pathlib import Path

import aiofiles
from nonebot import logger

from .config import ScopedConfig
from .memory import ConversationTurn, restore_turns
from .state import ChatState, StateStore


class StatePersistence:
    def __init__(self, config: ScopedConfig, states: StateStore, group_file: Path, private_file: Path):
        self.config = config
        self.states = states
        self.group_file = group_file
        self.private_file = private_file
        self._lock = asyncio.Lock()

    @staticmethod
    def _serialize(state: ChatState, include_probability: bool) -> dict:
        data = {
            "memory_version": 2,
            "preset": state.preset_name,
            "memory_summary": state.memory_summary,
            "turns": [turn.messages for turn in state.turns],
            "last_prompt_tokens": state.last_prompt_tokens,
            "last_cached_tokens": state.last_cached_tokens,
            "usage_preset_name": state.usage_preset_name,
            "last_active": state.last_active,
            "group_prompt": state.prompt,
            "output_reasoning_content": state.output_reasoning_content,
        }
        if include_probability:
            data["random_trigger_prob"] = state.random_trigger_prob
        return data

    async def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        async with aiofiles.open(temporary, "w", encoding="utf8") as file:
            await file.write(json.dumps(data, ensure_ascii=False))
        os.replace(temporary, path)

    async def save(self) -> None:
        async with self._lock:
            await self._write_json(
                self.group_file,
                {key: self._serialize(state, True) for key, state in self.states.group_states.items()},
            )
            if self.config.enable_private_chat:
                await self._write_json(
                    self.private_file,
                    {key: self._serialize(state, False) for key, state in self.states.private_states.items()},
                )

    async def _read_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            async with aiofiles.open(path, encoding="utf8") as file:
                value = json.loads(await file.read())
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            logger.exception(f"读取llmchat状态失败: {path}")
            return {}

    def _restore(self, raw: dict, *, is_group: bool) -> ChatState:
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

    async def load(self) -> None:
        groups = await self._read_json(self.group_file)
        for context_id, raw in groups.items():
            self.states.group_states[int(context_id)] = self._restore(raw, is_group=True)
        if self.config.enable_private_chat:
            private = await self._read_json(self.private_file)
            for context_id, raw in private.items():
                self.states.private_states[int(context_id)] = self._restore(raw, is_group=False)
