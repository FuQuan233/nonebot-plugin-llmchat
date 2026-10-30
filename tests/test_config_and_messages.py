# ruff: noqa: I001
import unittest
from io import BytesIO

from PIL import Image
from pydantic import ValidationError

import tests.bootstrap  # noqa: F401
from nonebot_plugin_llmchat.config import MCPServerConfig, PresetConfig, ScopedConfig
from nonebot_plugin_llmchat.message_utils import create_image_preview, pop_reasoning_content


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

    def test_image_preview_is_resized_and_converted_to_jpeg(self):
        source = Image.effect_noise((2048, 1024), 100).convert("RGB")
        original = BytesIO()
        source.save(original, format="PNG")

        preview, mime_type = create_image_preview(original.getvalue(), max_edge=512, quality=70)

        with Image.open(BytesIO(preview)) as image:
            assert image.format == "JPEG"
            assert image.size == (512, 256)
        assert mime_type == "image/jpeg"
        assert len(preview) < len(original.getvalue())

    def test_image_preview_uses_first_animation_frame(self):
        first = Image.new("RGB", (80, 40), "red")
        second = Image.new("RGB", (80, 40), "blue")
        original = BytesIO()
        first.save(original, format="GIF", save_all=True, append_images=[second], loop=0)

        preview, mime_type = create_image_preview(original.getvalue(), max_edge=512, quality=82)

        with Image.open(BytesIO(preview)) as image:
            pixel = image.convert("RGB").getpixel((20, 20))
            assert isinstance(pixel, tuple)
            red, green, blue = pixel
            assert image.format == "JPEG"
            assert red > blue
            assert green < red
        assert mime_type == "image/jpeg"
