"""把模型的按行回复解析为聊天消息。"""

NO_REPLY = "<<<LLMCHAT_NO_REPLY>>>"
BLOCK_FENCE = "```"


class ReplyLineParser:
    """增量解析普通文本行，并把 fenced content block 保留为单条消息。"""

    def __init__(self) -> None:
        self._pending = ""
        self._in_block = False
        self._block_lines: list[str] = []

    def feed(self, fragment: str) -> list[str]:
        self._pending += fragment
        messages: list[str] = []
        while boundary := self._next_boundary():
            index, marker = boundary
            line = self._pending[:index]
            self._pending = self._pending[index + len(marker) :]
            messages.extend(self._consume_line(line))

        # 内容块的结束标记无需等待下一个换行符。
        if self._in_block and self._pending.strip() == BLOCK_FENCE:
            self._pending = ""
            messages.extend(self._consume_line(BLOCK_FENCE))
        return messages

    def finish(self) -> list[str]:
        messages: list[str] = []
        if self._pending:
            messages.extend(self._consume_line(self._pending))
            self._pending = ""
        if self._in_block:
            messages.extend(self._flush_block())
        return messages

    def _next_boundary(self) -> tuple[int, str] | None:
        markers = ("\n",)
        matches = [(index, marker) for marker in markers if (index := self._pending.find(marker)) >= 0]
        return min(matches, key=lambda match: match[0]) if matches else None

    def _consume_line(self, line: str) -> list[str]:
        line = line.removesuffix("\r")
        stripped = line.strip()
        if self._in_block:
            if stripped == BLOCK_FENCE:
                return self._flush_block()
            self._block_lines.append(line)
            return []
        if stripped.startswith(BLOCK_FENCE):
            self._in_block = True
            self._block_lines = []
            return []
        if not stripped or stripped == NO_REPLY:
            return []
        return [stripped]

    def _flush_block(self) -> list[str]:
        block = "\n".join(self._block_lines).strip("\n")
        self._in_block = False
        self._block_lines = []
        return [block] if block else []


def parse_reply_segments(content: str) -> list[str]:
    """解析完整回复：普通行拆分，内容框保持为整体。"""
    parser = ReplyLineParser()
    return [*parser.feed(content), *parser.finish()]
