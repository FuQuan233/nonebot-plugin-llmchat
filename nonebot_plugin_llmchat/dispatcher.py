import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from nonebot import logger

from .state import ChatState

EventProcessor = Callable[[int, bool, ChatState, Any], Awaitable[None]]


class MessageDispatcher:
    """每个会话只运行一个worker，并可靠接管竞态窗口中新入队的消息。"""

    def __init__(self, processor: EventProcessor):
        self._processor = processor
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False

    async def enqueue(self, context_id: int, is_group: bool, state: ChatState, event: Any) -> None:
        if self._closing:
            return
        await state.queue.put(event)
        async with state.worker_lock:
            if state.worker_task is None or state.worker_task.done():
                self._start_worker(context_id, is_group, state)

    def _start_worker(self, context_id: int, is_group: bool, state: ChatState) -> None:
        task = asyncio.create_task(
            self._run_worker(context_id, is_group, state),
            name=f"llmchat:{'group' if is_group else 'private'}:{context_id}",
        )
        state.worker_task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_worker(self, context_id: int, is_group: bool, state: ChatState) -> None:
        current_task = asyncio.current_task()
        try:
            while True:
                try:
                    event = state.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    await self._processor(context_id, is_group, state, event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(f"处理会话 {context_id} 的消息失败")
                finally:
                    state.queue.task_done()
        finally:
            async with state.worker_lock:
                if state.worker_task is current_task:
                    state.worker_task = None
                if not self._closing and not state.queue.empty():
                    self._start_worker(context_id, is_group, state)

    async def shutdown(self) -> None:
        self._closing = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
