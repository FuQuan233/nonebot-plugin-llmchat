"""子模型调用模块

允许主模型通过 function tool 调用其他模型来完成特定任务（如生成图片、语音、视频）。
"""

import asyncio
import base64
import json
from typing import Any

import httpx
from nonebot import logger
from openai import AsyncOpenAI

from .config import PresetConfig, ScopedConfig


class SubModelCaller:
    """子模型调用管理器"""

    _instance = None
    _initialized = False

    def __new__(cls, plugin_config: ScopedConfig | None = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, plugin_config: ScopedConfig | None = None):
        if self._initialized:
            return

        if plugin_config is None:
            raise ValueError("plugin_config must be provided for first initialization")

        self.plugin_config = plugin_config
        self._preset_map: dict[str, PresetConfig] = {
            p.name: p for p in plugin_config.api_presets
        }
        self._initialized = True
        logger.info("SubModelCaller 初始化完成")

    @classmethod
    def get_instance(cls, plugin_config: ScopedConfig | None = None) -> "SubModelCaller":
        """获取单例实例"""
        if cls._instance is None:
            if plugin_config is None:
                raise ValueError("plugin_config must be provided for first initialization")
            cls._instance = cls(plugin_config)
        return cls._instance

    def _get_callable_presets(self, current_preset: PresetConfig) -> list[PresetConfig]:
        """获取当前预设可调用的子模型预设列表"""
        if not current_preset.call_model_list:
            return []

        callable_presets = []
        for name in current_preset.call_model_list:
            if name in self._preset_map:
                callable_presets.append(self._preset_map[name])
            else:
                logger.warning(f"call_model_list 中的模型 '{name}' 不存在于 api_presets 中")

        return callable_presets

    def _get_presets_with_capability(
        self,
        current_preset: PresetConfig,
        capability: str
    ) -> list[PresetConfig]:
        """获取具有特定能力的可调用子模型列表

        Args:
            current_preset: 当前主模型预设
            capability: 能力名称，如 'support_to_image'

        Returns:
            具有该能力的子模型预设列表（按 call_model_list 顺序）
        """
        callable_presets = self._get_callable_presets(current_preset)
        return [p for p in callable_presets if getattr(p, capability, False)]

    def get_available_tools(self, current_preset: PresetConfig) -> list[dict[str, Any]]:
        """根据当前预设的 call_model_list 动态生成可用的子模型调用工具

        只有当 call_model_list 中存在具有相应能力的模型时，才会生成对应的工具。
        """
        tools = []

        # 检查是否有可调用的图片生成模型
        image_models = self._get_presets_with_capability(current_preset, "support_to_image")
        if image_models:
            model_names = [m.name for m in image_models]
            tools.append({
                "type": "function",
                "function": {
                    "name": "submodel__generate_image",
                    "description": f"""调用子模型生成图片。可用的图片生成模型：{', '.join(model_names)}。
使用说明：
- 当用户要求生成图片时使用此工具
- prompt 应该是详细的图片描述，用英文效果更好
- 系统会自动选择最优的模型，如果失败会自动切换备选模型
- 返回结果包含 base64 编码的图片数据""",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "description": "图片生成提示词，描述要生成的图片内容，建议使用英文"
                            },
                            "preferred_model": {
                                "type": "string",
                                "description": f"可选：指定使用的模型名称，可选值：{', '.join(model_names)}",
                                "enum": model_names
                            }
                        },
                        "required": ["prompt"]
                    }
                }
            })

        # 检查是否有可调用的语音生成模型
        voice_models = self._get_presets_with_capability(current_preset, "support_to_voice")
        if voice_models:
            model_names = [m.name for m in voice_models]
            tools.append({
                "type": "function",
                "function": {
                    "name": "submodel__generate_voice",
                    "description": f"""调用子模型生成语音。可用的语音生成模型：{', '.join(model_names)}。
使用说明：
- 当用户要求生成语音或朗读文本时使用此工具
- text 是要转换为语音的文本内容
- 返回结果包含 base64 编码的音频数据""",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "要转换为语音的文本内容"
                            },
                            "preferred_model": {
                                "type": "string",
                                "description": f"可选：指定使用的模型名称，可选值：{', '.join(model_names)}",
                                "enum": model_names
                            }
                        },
                        "required": ["text"]
                    }
                }
            })

        # 检查是否有可调用的视频生成模型
        video_models = self._get_presets_with_capability(current_preset, "support_to_video")
        if video_models:
            model_names = [m.name for m in video_models]
            tools.append({
                "type": "function",
                "function": {
                    "name": "submodel__generate_video",
                    "description": f"""调用子模型生成视频。可用的视频生成模型：{', '.join(model_names)}。
使用说明：
- 当用户要求生成视频时使用此工具
- prompt 是视频内容描述
- 返回结果包含视频数据或URL""",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "description": "视频生成提示词，描述要生成的视频内容"
                            },
                            "preferred_model": {
                                "type": "string",
                                "description": f"可选：指定使用的模型名称，可选值：{', '.join(model_names)}",
                                "enum": model_names
                            }
                        },
                        "required": ["prompt"]
                    }
                }
            })

        return tools

    async def _call_model_api(
        self,
        preset: PresetConfig,
        messages: list[dict],
        tools: list[dict] | None = None
    ) -> dict[str, Any]:
        """调用模型 API

        Args:
            preset: 模型预设配置
            messages: 消息列表
            tools: 可选的工具列表（如果模型支持 MCP）

        Returns:
            包含响应内容的字典
        """
        # 初始化 OpenAI 客户端
        if preset.proxy:
            client = AsyncOpenAI(
                base_url=preset.api_base,
                api_key=preset.api_key,
                timeout=self.plugin_config.request_timeout,
                http_client=httpx.AsyncClient(proxy=preset.proxy),
            )
        else:
            client = AsyncOpenAI(
                base_url=preset.api_base,
                api_key=preset.api_key,
                timeout=self.plugin_config.request_timeout,
            )

        # 构建请求参数
        request_params = {
            "model": preset.model_name,
            "max_tokens": preset.max_tokens,
            "temperature": preset.temperature,
            "messages": messages
        }

        # 如果模型支持 MCP 并且提供了工具，添加到请求中
        if preset.support_mcp and tools:
            request_params["tools"] = tools

        response = await client.chat.completions.create(**request_params)
        message = response.choices[0].message

        result = {
            "content": message.content,
            "tool_calls": message.tool_calls,
            "images": getattr(message, "images", None),
            "audio": getattr(message, "audio", None),
            "video": getattr(message, "video", None),
        }

        return result

    async def _call_with_mcp_support(
        self,
        preset: PresetConfig,
        initial_messages: list[dict],
        mcp_tools: list[dict] | None = None
    ) -> dict[str, Any]:
        """调用模型并处理可能的 MCP 工具调用

        如果模型支持 MCP，会处理工具调用循环直到得到最终响应。
        """
        messages = initial_messages.copy()
        tools = mcp_tools if preset.support_mcp else None

        # 最多进行 5 轮工具调用
        max_tool_rounds = 5

        for _ in range(max_tool_rounds):
            result = await self._call_model_api(preset, messages, tools)

            # 如果没有工具调用，直接返回结果
            if not result["tool_calls"]:
                return result

            # 处理工具调用
            logger.info(f"子模型 {preset.name} 请求调用工具: {[tc.function.name for tc in result['tool_calls']]}")

            # 添加 assistant 消息
            messages.append({
                "role": "assistant",
                "tool_calls": [tc.model_dump() for tc in result["tool_calls"]]
            })

            # 处理每个工具调用
            for tool_call in result["tool_calls"]:
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)

                # 调用 MCP 工具
                try:
                    from .mcpclient import MCPClient
                    mcp_client = MCPClient.get_instance(self.plugin_config.mcp_servers)
                    tool_result = await mcp_client.call_tool(
                        tool_name,
                        tool_args,
                        group_id=None,
                        bot_id=None,
                        user_id=None,
                        is_group=False
                    )
                    result_str = str(tool_result) if tool_result else "工具调用成功"
                except Exception as e:
                    logger.error(f"子模型 MCP 工具调用失败: {e}")
                    result_str = f"工具调用失败: {e}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result_str
                })

        # 超过最大轮数，返回最后的结果
        logger.warning(f"子模型 {preset.name} 工具调用超过 {max_tool_rounds} 轮")
        return await self._call_model_api(preset, messages, None)

    async def generate_image(
        self,
        current_preset: PresetConfig,
        prompt: str,
        preferred_model: str | None = None
    ) -> dict[str, Any]:
        """生成图片

        Args:
            current_preset: 当前主模型预设
            prompt: 图片生成提示词
            preferred_model: 可选的指定模型名称

        Returns:
            包含生成结果的字典：
            - success: bool
            - images: list[str] (base64 编码的图片)
            - content: str (模型的文本回复)
            - error: str (如果失败)
            - model_used: str (实际使用的模型名称)
        """
        image_models = self._get_presets_with_capability(current_preset, "support_to_image")

        if not image_models:
            return {
                "success": False,
                "error": "没有可用的图片生成模型",
                "images": [],
                "content": ""
            }

        # 如果指定了模型，调整顺序
        if preferred_model:
            image_models = sorted(
                image_models,
                key=lambda p: 0 if p.name == preferred_model else 1
            )

        # 获取 MCP 工具（如果需要）
        mcp_tools = None
        try:
            from .mcpclient import MCPClient
            mcp_client = MCPClient.get_instance(self.plugin_config.mcp_servers)
            await mcp_client.init_tools_cache()
            mcp_tools = mcp_client._tools_cache.copy() if mcp_client._tools_cache else None
        except Exception as e:
            logger.debug(f"获取 MCP 工具失败: {e}")

        # 构建消息
        messages = [
            {
                "role": "system",
                "content": "你是一个图片生成助手。请根据用户的描述生成图片。直接生成图片，不需要额外解释。"
            },
            {
                "role": "user",
                "content": prompt
            }
        ]

        errors = []
        for preset in image_models:
            logger.info(f"尝试使用模型 {preset.name} 生成图片")
            try:
                result = await self._call_with_mcp_support(preset, messages, mcp_tools)

                # 检查是否有图片返回
                images = result.get("images")
                if images:
                    # 提取 base64 图片数据
                    image_list = []
                    for img in images:
                        if isinstance(img, dict) and "image_url" in img:
                            url = img["image_url"].get("url", "")
                            # 移除 data URL 前缀
                            if url.startswith("data:"):
                                # 格式: data:image/png;base64,xxxxx
                                base64_data = url.split(",", 1)[-1] if "," in url else url
                            else:
                                base64_data = url
                            image_list.append(base64_data)
                        elif isinstance(img, str):
                            image_list.append(img)

                    if image_list:
                        logger.info(f"模型 {preset.name} 成功生成 {len(image_list)} 张图片")
                        return {
                            "success": True,
                            "images": image_list,
                            "content": result.get("content", ""),
                            "model_used": preset.name
                        }

                # 没有图片但有内容，可能是模型回复了文本
                if result.get("content"):
                    logger.warning(f"模型 {preset.name} 返回了文本但没有图片")
                    errors.append(f"{preset.name}: 模型未生成图片")
                else:
                    errors.append(f"{preset.name}: 模型无响应")

            except Exception as e:
                logger.error(f"模型 {preset.name} 调用失败: {e}")
                errors.append(f"{preset.name}: {str(e)}")
                continue

        # 所有模型都失败了
        return {
            "success": False,
            "error": f"所有模型都无法生成图片。详情：{'; '.join(errors)}",
            "images": [],
            "content": ""
        }

    async def generate_voice(
        self,
        current_preset: PresetConfig,
        text: str,
        preferred_model: str | None = None
    ) -> dict[str, Any]:
        """生成语音

        Args:
            current_preset: 当前主模型预设
            text: 要转换为语音的文本
            preferred_model: 可选的指定模型名称

        Returns:
            包含生成结果的字典
        """
        voice_models = self._get_presets_with_capability(current_preset, "support_to_voice")

        if not voice_models:
            return {
                "success": False,
                "error": "没有可用的语音生成模型",
                "audio": None,
                "content": ""
            }

        if preferred_model:
            voice_models = sorted(
                voice_models,
                key=lambda p: 0 if p.name == preferred_model else 1
            )

        messages = [
            {
                "role": "system",
                "content": "你是一个语音生成助手。请将用户提供的文本转换为语音。"
            },
            {
                "role": "user",
                "content": f"请将以下文本转换为语音：\n{text}"
            }
        ]

        errors = []
        for preset in voice_models:
            logger.info(f"尝试使用模型 {preset.name} 生成语音")
            try:
                result = await self._call_with_mcp_support(preset, messages, None)

                audio = result.get("audio")
                if audio:
                    logger.info(f"模型 {preset.name} 成功生成语音")
                    return {
                        "success": True,
                        "audio": audio,
                        "content": result.get("content", ""),
                        "model_used": preset.name
                    }

                errors.append(f"{preset.name}: 模型未生成语音")

            except Exception as e:
                logger.error(f"模型 {preset.name} 调用失败: {e}")
                errors.append(f"{preset.name}: {str(e)}")
                continue

        return {
            "success": False,
            "error": f"所有模型都无法生成语音。详情：{'; '.join(errors)}",
            "audio": None,
            "content": ""
        }

    async def generate_video(
        self,
        current_preset: PresetConfig,
        prompt: str,
        preferred_model: str | None = None
    ) -> dict[str, Any]:
        """生成视频

        Args:
            current_preset: 当前主模型预设
            prompt: 视频生成提示词
            preferred_model: 可选的指定模型名称

        Returns:
            包含生成结果的字典
        """
        video_models = self._get_presets_with_capability(current_preset, "support_to_video")

        if not video_models:
            return {
                "success": False,
                "error": "没有可用的视频生成模型",
                "video": None,
                "content": ""
            }

        if preferred_model:
            video_models = sorted(
                video_models,
                key=lambda p: 0 if p.name == preferred_model else 1
            )

        messages = [
            {
                "role": "system",
                "content": "你是一个视频生成助手。请根据用户的描述生成视频。"
            },
            {
                "role": "user",
                "content": prompt
            }
        ]

        errors = []
        for preset in video_models:
            logger.info(f"尝试使用模型 {preset.name} 生成视频")
            try:
                result = await self._call_with_mcp_support(preset, messages, None)

                video = result.get("video")
                if video:
                    logger.info(f"模型 {preset.name} 成功生成视频")
                    return {
                        "success": True,
                        "video": video,
                        "content": result.get("content", ""),
                        "model_used": preset.name
                    }

                errors.append(f"{preset.name}: 模型未生成视频")

            except Exception as e:
                logger.error(f"模型 {preset.name} 调用失败: {e}")
                errors.append(f"{preset.name}: {str(e)}")
                continue

        return {
            "success": False,
            "error": f"所有模型都无法生成视频。详情：{'; '.join(errors)}",
            "video": None,
            "content": ""
        }

    async def call_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        current_preset: PresetConfig
    ) -> dict[str, Any]:
        """工具调用入口

        Args:
            tool_name: 工具名称
            tool_args: 工具参数
            current_preset: 当前主模型预设

        Returns:
            工具调用结果
        """
        if tool_name == "submodel__generate_image":
            return await self.generate_image(
                current_preset=current_preset,
                prompt=tool_args.get("prompt", ""),
                preferred_model=tool_args.get("preferred_model")
            )
        elif tool_name == "submodel__generate_voice":
            return await self.generate_voice(
                current_preset=current_preset,
                text=tool_args.get("text", ""),
                preferred_model=tool_args.get("preferred_model")
            )
        elif tool_name == "submodel__generate_video":
            return await self.generate_video(
                current_preset=current_preset,
                prompt=tool_args.get("prompt", ""),
                preferred_model=tool_args.get("preferred_model")
            )
        else:
            return {
                "success": False,
                "error": f"未知的子模型工具: {tool_name}"
            }

    def get_friendly_name(self, tool_name: str) -> str:
        """获取工具的友好名称"""
        friendly_names = {
            "submodel__generate_image": "子模型 - 生成图片",
            "submodel__generate_voice": "子模型 - 生成语音",
            "submodel__generate_video": "子模型 - 生成视频",
        }
        return friendly_names.get(tool_name, tool_name)
