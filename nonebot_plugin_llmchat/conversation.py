import asyncio
import base64
from collections import Counter
import json
from typing import Any, cast

import httpx
from nonebot import get_bot, logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from openai import AsyncOpenAI

from .config import PresetConfig, ScopedConfig
from .mcpclient import MCPClient
from .message_utils import (
    ChatEvent,
    MessageSender,
    build_reasoning_forward_nodes,
    download_images,
    format_message,
    pop_reasoning_content,
    send_reply_messages,
)
from .prompts import build_system_prompt
from .state import ChatState, StateStore
from .streaming import CompletionResult, StreamedMessageBuilder, StreamingReplyEmitter


class ConversationService:
    def __init__(
        self,
        config: ScopedConfig,
        states: StateStore,
        bot_names: set[str],
        sender: MessageSender,
    ) -> None:
        self.config = config
        self.states = states
        self.bot_names = bot_names
        self.sender = sender

    async def process_event(
        self,
        context_id: int,
        is_group: bool,
        state: ChatState,
        event: ChatEvent,
    ) -> None:
        snapshot = list(state.pending_events)
        if not snapshot:
            logger.debug(f"会话 {context_id} 没有待处理上下文，跳过重复触发")
            return
        state.pending_events.clear()
        state.last_active = event.time
        try:
            await self._process_snapshot(context_id, is_group, state, event, snapshot)
        except asyncio.CancelledError:
            state.pending_events.extendleft(reversed(snapshot))
            raise
        except Exception as error:
            state.pending_events.extendleft(reversed(snapshot))
            logger.opt(exception=error).error(f"API请求失败 会话：{context_id}")
            await self.sender(Message(f"服务暂时不可用，请稍后再试\n{error!s}"))

    def _create_client(self, preset: PresetConfig) -> AsyncOpenAI:
        options: dict[str, Any] = {
            "base_url": preset.api_base,
            "api_key": preset.api_key,
            "timeout": self.config.request_timeout,
        }
        if preset.user_agent:
            options["default_headers"] = {"User-Agent": preset.user_agent}
        if preset.proxy:
            options["http_client"] = httpx.AsyncClient(proxy=preset.proxy)
        return AsyncOpenAI(**options)

    async def _process_snapshot(
        self,
        context_id: int,
        is_group: bool,
        state: ChatState,
        event: ChatEvent,
        events: list[ChatEvent],
    ) -> None:
        preset = self.states.get_preset(state)
        mcp_client = MCPClient.get_instance(
            self.config.mcp_servers,
            self.config.mcp_server_cwd,
            self.config.mcp_timeout,
        )
        system_prompt = build_system_prompt(
            config=self.config,
            state=state,
            bot_names=self.bot_names,
            is_group=is_group,
            support_tools=preset.support_mcp,
        )
        while state.history and state.history[0].get("role") != "user":
            state.history.popleft()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *list(state.history)[-self.config.history_size * 2 :],
        ]
        content: list[dict[str, Any]] = []
        bot_name = next(iter(sorted(self.bot_names)), "机器人")
        for pending_event in events:
            content.append({"type": "text", "text": format_message(pending_event, bot_name)})
            if preset.support_image:
                for image in await download_images(pending_event):
                    content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}})
        transcript: list[dict[str, Any]] = [{"role": "user", "content": content}]
        request_config: dict[str, Any] = {
            "model": preset.model_name,
            "max_tokens": preset.max_tokens,
            "temperature": preset.temperature,
            "timeout": self.config.request_timeout,
            "extra_body": preset.extra_body,
        }
        if preset.support_mcp:
            request_config["tools"] = await mcp_client.get_available_tools(is_group)

        client = self._create_client(preset)
        try:
            completion = await self._run_tool_loop(
                client=client,
                preset=preset,
                mcp_client=mcp_client,
                request_config=request_config,
                messages=messages,
                transcript=transcript,
                event=event,
                is_group=is_group,
            )
        finally:
            await client.close()

        message = completion.message
        reply, tagged_reasoning = pop_reasoning_content(getattr(message, "content", None))
        reasoning = getattr(message, "reasoning_content", None) or tagged_reasoning
        assistant_message: dict[str, Any] = {"role": "assistant", "content": reply or ""}
        reply_images = getattr(message, "images", None)
        if reply_images:
            assistant_message["images"] = reply_images
        if preset.request_with_reasoning_content:
            assistant_message["reasoning_content"] = reasoning
        transcript.append(assistant_message)
        state.history.extend(transcript)

        if state.output_reasoning_content and reasoning:
            await self._send_reasoning(context_id, is_group, event, reasoning)
        if reply and not completion.streamed:
            await send_reply_messages(self.sender, reply)
        await self._send_images(reply_images)

    async def _complete(
        self,
        client: AsyncOpenAI,
        request_config: dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        stream: bool,
    ) -> CompletionResult:
        completion_config = dict(request_config)
        if stream:
            completion_config["stream"] = True
        response = await cast(Any, client.chat.completions.create)(**completion_config, messages=messages)
        if not stream:
            if not response.choices:
                raise RuntimeError("API响应中没有choices")
            if response.usage is not None:
                logger.debug(f"API响应token数：{response.usage.total_tokens}")
            return CompletionResult(message=response.choices[0].message)

        builder = StreamedMessageBuilder()
        emitter = StreamingReplyEmitter(self._send_stream_segment)
        async for chunk in response:
            content_fragment = builder.add_chunk(chunk)
            if content_fragment:
                await emitter.feed(content_fragment)
        await emitter.finish()
        if builder.usage is not None:
            logger.debug(f"API流式响应token数：{builder.usage.total_tokens}")
        return CompletionResult(message=builder.build(), streamed=True)

    async def _send_stream_segment(self, content: str) -> None:
        logger.debug(f"流式消息完成，立即发送：{content[:50]}")
        await self.sender(Message(content))

    async def _run_tool_loop(
        self,
        *,
        client: AsyncOpenAI,
        preset: PresetConfig,
        mcp_client: MCPClient,
        request_config: dict[str, Any],
        messages: list[dict[str, Any]],
        transcript: list[dict[str, Any]],
        event: ChatEvent,
        is_group: bool,
    ) -> CompletionResult:
        completion = await self._complete(client, request_config, messages + transcript, stream=preset.stream)
        message = completion.message
        if not preset.support_mcp:
            return completion

        call_counts: Counter[str] = Counter()
        for round_number in range(1, self.config.max_tool_rounds + 1):
            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                return completion
            logger.info(f"处理第 {round_number}/{self.config.max_tool_rounds} 轮工具调用")
            assistant_reply: dict[str, Any] = {
                "role": "assistant",
                "content": getattr(message, "content", None),
                "tool_calls": [tool_call.model_dump() for tool_call in tool_calls],
            }
            reasoning = getattr(message, "reasoning_content", None)
            if preset.request_with_reasoning_content and reasoning is not None:
                assistant_reply["reasoning_content"] = reasoning
            transcript.append(assistant_reply)
            if getattr(message, "content", None) and not completion.streamed:
                await send_reply_messages(self.sender, message.content)

            for tool_call in tool_calls:
                await self._handle_tool_call(
                    mcp_client,
                    transcript,
                    tool_call,
                    call_counts,
                    event,
                    is_group,
                )
            completion = await self._complete(client, request_config, messages + transcript, stream=preset.stream)
            message = completion.message

        if not getattr(message, "tool_calls", None):
            return completion
        logger.warning(f"工具调用达到上限 {self.config.max_tool_rounds}，强制要求模型总结")
        final_config = {key: value for key, value in request_config.items() if key not in {"tools", "tool_choice"}}
        final_messages = [
            *messages,
            *transcript,
            {
                "role": "system",
                "content": "工具调用次数已达上限。不得再调用工具，请根据已有结果直接给出最终回答。",
            },
        ]
        return await self._complete(client, final_config, final_messages, stream=preset.stream)

    async def _handle_tool_call(
        self,
        mcp_client: MCPClient,
        transcript: list[dict[str, Any]],
        tool_call: Any,
        call_counts: Counter[str],
        event: ChatEvent,
        is_group: bool,
    ) -> None:
        name = tool_call.function.name
        raw_arguments = tool_call.function.arguments
        try:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise TypeError("arguments必须是JSON对象")
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            result = f"工具参数格式错误: {error!s}"
        else:
            signature = f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
            call_counts[signature] += 1
            if call_counts[signature] > self.config.max_repeated_tool_calls:
                result = "相同工具和参数的调用已达到上限，请使用已有结果继续回答。"
                logger.warning(f"阻止重复工具调用: {signature}")
            else:
                await self.sender(Message(f"正在使用{mcp_client.get_friendly_name(name)}"))
                result = await mcp_client.call_tool(
                    name,
                    arguments,
                    group_id=event.group_id if is_group and isinstance(event, GroupMessageEvent) else None,
                    bot_id=str(event.self_id),
                )
        transcript.append({"role": "tool", "tool_call_id": tool_call.id, "content": str(result)})

    async def _send_reasoning(self, context_id: int, is_group: bool, event: ChatEvent, content: str) -> None:
        try:
            bot = get_bot(str(event.self_id))
            nickname = next(iter(sorted(self.bot_names)), "机器人")
            nodes = build_reasoning_forward_nodes(bot.self_id, nickname, content)
            if is_group:
                await bot.send_group_forward_msg(group_id=context_id, messages=nodes)
            else:
                await bot.send_private_forward_msg(user_id=context_id, messages=nodes)
        except Exception:
            logger.exception("合并转发思维内容失败")

    async def _send_images(self, images: Any) -> None:
        for image in images or []:
            encoded = image["image_url"]["url"].split(",", maxsplit=1)[-1]
            await self.sender(Message(MessageSegment.image(base64.b64decode(encoded))))
