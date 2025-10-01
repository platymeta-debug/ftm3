import asyncio
from typing import Any, Dict


class EventBus:
    """Simple asynchronous event bus built on top of asyncio.Queue."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        print("이벤트 버스가 초기화되었습니다.")

    async def publish(self, event_type: str, data: Dict[str, Any]) -> None:
        """Publish a new event into the queue."""
        await self.queue.put({"type": event_type, "data": data})

    async def subscribe(self) -> Dict[str, Any]:
        """Wait for and return the next available event."""
        return await self.queue.get()

    def task_done(self) -> None:
        """Mark the current event as processed."""
        self.queue.task_done()


# 단일 이벤트 버스 객체
event_bus = EventBus()
