# ruff: noqa: I001
import unittest

from pydantic import ValidationError

import tests.bootstrap  # noqa: F401
from nonebot_plugin_llmchat.config import MCPServerConfig, PresetConfig, ScopedConfig
from nonebot_plugin_llmchat.message_utils import pop_reasoning_content


def assert_validation_error(factory):
    try:
        factory()
    except ValidationError:
        return
    raise AssertionError("expected ValidationError")


class ConfigurationTests(unittest.TestCase):
    def test_rejects_duplicate_presets(self):
        preset = PresetConfig(name="same", api_base="x", api_key="x", model_name="x")
        assert_validation_error(lambda: ScopedConfig(api_presets=[preset, preset]))

    def test_mcp_requires_exactly_one_transport_target(self):
        assert_validation_error(MCPServerConfig)
        assert_validation_error(lambda: MCPServerConfig(command="cmd", url="https://example.invalid"))

    def test_reasoning_tag_is_removed(self):
        reply, reasoning = pop_reasoning_content("<think>secret</think>answer")
        assert reply == "answer"
        assert reasoning == "secret"
