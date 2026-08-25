import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
import time
from typing import Any

from .config import PresetConfig, ScopedConfig
from .memory import ConversationTurn, flatten_turns


@dataclass
class ChatState:
    preset_name: str
    past_events_size: int
    random_trigger_prob: float = 0.0
    turns: deque[ConversationTurn] = field(default_factory=deque)
    pending_events: deque[Any] = field(init=False)
    queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    worker_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    worker_task: asyncio.Task[None] | None = None
    last_active: float = field(default_factory=time.time)
    prompt: str | None = None
    output_reasoning_content: bool = False
    memory_summary: str | None = None
    last_prompt_tokens: int | None = None
    last_cached_tokens: int | None = None
    usage_preset_name: str | None = None

    def __post_init__(self) -> None:
        self.pending_events = deque(maxlen=self.past_events_size)

    @property
    def history(self) -> list[dict[str, Any]]:
        """兼容旧版只读扁平历史访问。"""
        return flatten_turns(list(self.turns))

    def clear_memory(self) -> None:
        self.turns.clear()
        self.memory_summary = None
        self.clear_usage()

    def clear_usage(self) -> None:
        self.last_prompt_tokens = None
        self.last_cached_tokens = None
        self.usage_preset_name = None


class StateStore:
    def __init__(self, config: ScopedConfig):
        self.config = config
        self.group_states: dict[int, ChatState] = defaultdict(self._new_group_state)
        self.private_states: dict[int, ChatState] = defaultdict(self._new_private_state)

    def _new_group_state(self) -> ChatState:
        return ChatState(
            preset_name=self.config.default_preset,
            past_events_size=self.config.past_events_size,
            random_trigger_prob=self.config.random_trigger_prob,
        )

    def _new_private_state(self) -> ChatState:
        return ChatState(
            preset_name=self.config.private_chat_preset,
            past_events_size=self.config.past_events_size,
        )

    def get(self, context_id: int, is_group: bool) -> ChatState:
        return self.group_states[context_id] if is_group else self.private_states[context_id]

    def get_preset(self, state: ChatState) -> PresetConfig:
        return next(
            (preset for preset in self.config.api_presets if preset.name == state.preset_name),
            self.config.api_presets[0],
        )
