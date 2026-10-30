# ruff: noqa: I001
import unittest

import tests.bootstrap  # noqa: F401
from nonebot_plugin_llmchat.config import PresetConfig, ScopedConfig
from nonebot_plugin_llmchat.output_protocol import NO_REPLY, parse_reply_segments
from nonebot_plugin_llmchat.prompts import build_system_prompt
from nonebot_plugin_llmchat.state import StateStore


class OutputProtocolTests(unittest.TestCase):
    def test_each_nonempty_text_line_is_a_message(self):
        assert parse_reply_segments("第一条\n第二条\n\n第三条") == ["第一条", "第二条", "第三条"]

    def test_no_reply_marker_never_becomes_a_chat_message(self):
        assert parse_reply_segments(NO_REPLY) == []
        assert parse_reply_segments(f"answer\n{NO_REPLY}") == ["answer"]

    def test_commas_are_not_split_by_code(self):
        content = "好啊，没问题,我马上来"
        assert parse_reply_segments(content) == [content]

    def test_fenced_code_is_one_message_without_fence_or_language(self):
        content = '我来写\n```python\nprint("hello")\nprint("world")\n```\n好了'
        assert parse_reply_segments(content) == [
            "我来写",
            'print("hello")\nprint("world")',
            "好了",
        ]

    def test_fenced_long_text_preserves_paragraphs_as_one_message(self):
        content = "前言\n```text\n第一段内容。\n\n第二段内容。\n```\n结尾"
        assert parse_reply_segments(content) == [
            "前言",
            "第一段内容。\n\n第二段内容。",
            "结尾",
        ]

    def test_unclosed_content_block_is_flushed_as_one_message(self):
        content = "```text\nline 1\n\nline 3"
        assert parse_reply_segments(content) == ["line 1\n\nline 3"]

    def test_prompt_distinguishes_short_messages_and_atomic_content_blocks(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x")
        config = ScopedConfig(api_presets=[preset], default_preset="test")
        prompt = build_system_prompt(
            config=config,
            state=StateStore(config).group_states[1],
            bot_names={"bot"},
            is_group=True,
            support_tools=False,
        )

        assert "[短消息]" in prompt
        assert "一行就是一条聊天消息" in prompt
        assert "[原子内容块]" in prompt
        assert "不仅用于代码" in prompt
        assert "需要连贯阅读的长文本，必须完整放进一个内容框" in prompt
        assert "框内可以使用换行、空行和段落" in prompt
        assert "不要把长文本压成一行" in prompt
        assert "```text" in prompt
        assert "```python" in prompt
        assert NO_REPLY in prompt

    def test_prompt_encourages_visual_model_to_collect_and_send_stickers(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x", support_image=True)
        config = ScopedConfig(api_presets=[preset], default_preset="test")
        prompt = build_system_prompt(
            config=config,
            state=StateStore(config).group_states[1],
            bot_names={"bot"},
            is_group=True,
            support_tools=False,
            support_stickers=True,
        )

        assert "所有对话共享" in prompt
        assert "积极收藏和使用" in prompt
        assert "一次可以收藏任意多张" in prompt
        assert "可以根据表达需要连续发送" in prompt
        assert "会立即尝试匹配并发送" in prompt
