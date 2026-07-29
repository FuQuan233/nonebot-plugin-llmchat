# ruff: noqa: I001
import tempfile
from pathlib import Path
import unittest

import tests.bootstrap  # noqa: F401
from nonebot_plugin_llmchat.config import PresetConfig, ScopedConfig
from nonebot_plugin_llmchat.persistence import StatePersistence
from nonebot_plugin_llmchat.state import StateStore


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_state_loads_even_when_group_file_is_missing(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x")
        config = ScopedConfig(
            api_presets=[preset],
            default_preset="test",
            enable_private_chat=True,
            private_chat_preset="test",
        )
        with tempfile.TemporaryDirectory() as directory:
            group_file = Path(directory) / "groups.json"
            private_file = Path(directory) / "private.json"
            source = StateStore(config)
            source.private_states[42].prompt = "remember me"
            persistence = StatePersistence(config, source, group_file, private_file)
            await persistence.save()
            group_file.unlink()

            restored = StateStore(config)
            await StatePersistence(config, restored, group_file, private_file).load()

            assert restored.private_states[42].prompt == "remember me"
            assert not group_file.exists()
