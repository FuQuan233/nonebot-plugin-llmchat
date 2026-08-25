# ruff: noqa: I001
import json
import tempfile
from pathlib import Path
import unittest

import tests.bootstrap  # noqa: F401
from nonebot_plugin_llmchat.config import PresetConfig, ScopedConfig
from nonebot_plugin_llmchat.persistence import StatePersistence
from nonebot_plugin_llmchat.memory import ConversationTurn
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

    async def test_old_flat_history_is_migrated_and_saved_as_memory_v2(self):
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
            states = StateStore(config)
            persistence = StatePersistence(config, states, group_file, private_file)

            await persistence.load()

            assert len(states.group_states[1].turns) == 1
            assert [message["role"] for message in states.group_states[1].turns[0].messages] == [
                "user",
                "assistant",
                "tool",
                "assistant",
            ]
            states.group_states[1].memory_summary = "summary"
            states.group_states[1].last_prompt_tokens = 1234
            states.group_states[1].usage_preset_name = "test"
            await persistence.save()

            saved = json.loads(group_file.read_text(encoding="utf8"))["1"]
            assert saved["memory_version"] == 2
            assert saved["memory_summary"] == "summary"
            assert saved["last_prompt_tokens"] == 1234

    async def test_memory_v2_round_trip(self):
        preset = PresetConfig(name="test", api_base="x", api_key="x", model_name="x")
        config = ScopedConfig(api_presets=[preset], default_preset="test")
        with tempfile.TemporaryDirectory() as directory:
            group_file = Path(directory) / "groups.json"
            private_file = Path(directory) / "private.json"
            source = StateStore(config)
            source.group_states[1].turns.append(
                ConversationTurn([{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}])
            )
            source.group_states[1].memory_summary = "remember"
            await StatePersistence(config, source, group_file, private_file).save()

            restored = StateStore(config)
            await StatePersistence(config, restored, group_file, private_file).load()

            assert restored.group_states[1].memory_summary == "remember"
            assert restored.group_states[1].turns[0].messages[-1]["content"] == "hi"
