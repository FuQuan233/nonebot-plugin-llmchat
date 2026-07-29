import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
import time
from typing import Any

from .config import PresetConfig, ScopedConfig


@dataclass
class ChatState:
    preset_name: str
    history_size: int
    past_events_size: int
    random_trigger_prob: float = 0.0
    history: deque[dict[str, Any]] = field(init=False)
    pending_events: deque[Any] = field(init=False)
    queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    worker_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    worker_task: asyncio.Task[None] | None = None
    last_active: float = field(default_factory=time.time)
    prompt: str | None = None
    output_reasoning_content: bool = False

    def __post_init__(self) -> None:
        self.history = deque(maxlen=self.history_size * 2)
        self.pending_events = deque(maxlen=self.past_events_size)


class StateStore:
    def __init__(self, config: ScopedConfig):
        self.config = config
        self.group_states: dict[int, ChatState] = defaultdict(self._new_group_state)
        self.private_states: dict[int, ChatState] = defaultdict(self._new_private_state)

    def _new_group_state(self) -> ChatState:
        return ChatState(
            self.config.default_preset,
            self.config.history_size,
            self.config.past_events_size,
            self.config.random_trigger_prob,
        )

    def _new_private_state(self) -> ChatState:
        return ChatState(
            self.config.private_chat_preset,
            self.config.history_size,
            self.config.past_events_size,
        )

    def get(self, context_id: int, is_group: bool) -> ChatState:
        return self.group_states[context_id] if is_group else self.private_states[context_id]

    def get_preset(self, state: ChatState) -> PresetConfig:
        return next(
            (preset for preset in self.config.api_presets if preset.name == state.preset_name),
            self.config.api_presets[0],
        )
