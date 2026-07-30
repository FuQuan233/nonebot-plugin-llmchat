"""OpenAI ChatCompletion 流式响应的聚合与按行发送。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from .output_protocol import ReplyLineParser

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


@dataclass
class CompletionResult:
    message: Any
    streamed: bool = False


@dataclass
class StreamToolFunction:
    name: str = ""
    arguments: str = ""


@dataclass
class StreamToolCall:
    index: int
    id: str = ""
    type: str = "function"
    function: StreamToolFunction = field(default_factory=StreamToolFunction)

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


class StreamedMessageBuilder:
    """把 ChatCompletionChunk 增量还原成普通 assistant message。"""

    def __init__(self) -> None:
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._tool_calls: dict[int, StreamToolCall] = {}
        self._images: list[Any] = []
        self.usage: Any = None

    def add_chunk(self, chunk: Any) -> str:
        if getattr(chunk, "usage", None) is not None:
            self.usage = chunk.usage
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return ""
        delta = choices[0].delta
        content = getattr(delta, "content", None)
        content_fragment = content if isinstance(content, str) else ""
        if content_fragment:
            self._content.append(content_fragment)
        reasoning = getattr(delta, "reasoning_content", None)
        if isinstance(reasoning, str):
            self._reasoning.append(reasoning)
        images = getattr(delta, "images", None)
        if images:
            self._images.extend(images)
        for tool_delta in getattr(delta, "tool_calls", None) or []:
            index = tool_delta.index
            tool_call = self._tool_calls.setdefault(index, StreamToolCall(index=index))
            if getattr(tool_delta, "id", None):
                tool_call.id = tool_delta.id
            if getattr(tool_delta, "type", None):
                tool_call.type = tool_delta.type
            function = getattr(tool_delta, "function", None)
            if function is not None:
                if getattr(function, "name", None):
                    tool_call.function.name += function.name
                if getattr(function, "arguments", None):
                    tool_call.function.arguments += function.arguments
        return content_fragment

    def build(self) -> Any:
        content = "".join(self._content) or None
        reasoning = "".join(self._reasoning) or None
        tool_calls = [self._tool_calls[index] for index in sorted(self._tool_calls)] or None
        return SimpleNamespace(
            content=content,
            reasoning_content=reasoning,
            tool_calls=tool_calls,
            images=self._images or None,
        )


class StreamingReplyEmitter:
    """普通行完成时立即发送；内容块闭合后作为一条消息发送。"""

    def __init__(self, emit: Callable[[str], Awaitable[None]]) -> None:
        self._emit = emit
        self._parser = ReplyLineParser()
        self._prefix = ""
        self._think_state = "undecided"

    async def feed(self, fragment: str) -> None:
        visible = self._filter_reasoning(fragment)
        if visible:
            await self._emit_all(self._parser.feed(visible))

    async def finish(self) -> None:
        if self._think_state == "undecided" and self._prefix:
            await self._emit_all(self._parser.feed(self._prefix))
            self._prefix = ""
        await self._emit_all(self._parser.finish())

    async def _emit_all(self, messages: list[str]) -> None:
        for message in messages:
            await self._emit(message)

    def _filter_reasoning(self, fragment: str) -> str:
        if self._think_state == "visible":
            return fragment
        self._prefix += fragment
        if self._think_state == "undecided":
            if _THINK_OPEN.startswith(self._prefix):
                return ""
            if not self._prefix.startswith(_THINK_OPEN):
                self._think_state = "visible"
                visible, self._prefix = self._prefix, ""
                return visible
            self._think_state = "reasoning"
            self._prefix = self._prefix[len(_THINK_OPEN) :]
        if self._think_state == "reasoning":
            closing_index = self._prefix.find(_THINK_CLOSE)
            if closing_index < 0:
                return ""
            visible = self._prefix[closing_index + len(_THINK_CLOSE) :]
            self._prefix = ""
            self._think_state = "visible"
            return visible
        return ""
