import asyncio
import threading
from collections import defaultdict
from typing import Optional, Awaitable, TypeVar, Iterable, List

_T = TypeVar("_T")


class SyncAndAsyncEvent(threading.Event):
    async def wait_async(self, timeout: Optional[float] = None) -> bool:
        loop = asyncio.get_running_loop()

        try:
            await wait_with_timeout(
                loop.run_in_executor(None, self.wait),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            pass
        finally:
            return self.is_set()


class AsyncEventProvider:  # TODO: refactor
    def __init__(self) -> None:
        self._events: defaultdict[str, List[asyncio.Event]] = defaultdict(list)

    def _register_event(self, event_name: str, event: Optional[asyncio.Event] = None) -> asyncio.Event:
        if event is None:
            event = asyncio.Event()

        self._events[event_name].append(event)
        return event

    def _remove_event(self, event_name: str, event: asyncio.Event) -> None:
        try:
            self._events[event_name].remove(event)
        except ValueError:  # Safe to ignore. The event was already removed
            pass

    async def wait_for_event(self, event_name: str, *, timeout: Optional[float] = None) -> None:
        event = self._register_event(event_name)

        try:
            await wait_with_timeout(event.wait(), timeout=timeout)
        except asyncio.TimeoutError as error:
            self._remove_event(event_name, event)
            raise error

    async def wait_for_events(self, event_names: Iterable[str], *, timeout: Optional[float] = None) -> None:
        events = [self._register_event(event_name) for event_name in event_names]

        try:
            await asyncio.wait([asyncio.create_task(event.wait()) for event in events], timeout=timeout)
        except asyncio.TimeoutError as error:
            for event_name, event in zip(event_names, events):
                self._remove_event(event_name, event)

            raise error

    def dispatch_event(self, event_name: str) -> None:
        events = self._events.pop(event_name, None)

        if events is None:
            return

        for event in events:
            event.set()


async def wait_with_timeout(coro: Awaitable[_T], timeout: Optional[float] = None, shield: bool = False) -> _T:
    return await asyncio.wait_for(
        asyncio.shield(coro) if shield else coro,
        timeout=timeout
    )
