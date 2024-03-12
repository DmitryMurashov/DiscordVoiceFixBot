import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TypeAlias, Callable, Dict, Any, Coroutine, TypeVar, TYPE_CHECKING, Optional, Generic, Union

import aiohttp
from aiohttp import ClientWebSocketResponse
from discord import SpeakingState, ConnectionClosed
from discord.gateway import VoiceKeepAliveHandler
from discord.utils import _to_json, _from_json

if TYPE_CHECKING:
    from .connection_state import BaseCustomVoiceConnectionState  # type: ignore
    from discord.types.voice import SupportedModes

_voiceConnectionStateT = TypeVar('_voiceConnectionStateT', bound='BaseCustomVoiceConnectionState')
_logger = logging.getLogger(f"custom_voice.{__name__}")


class BaseCustomVoiceWebSocket(ABC, Generic[_voiceConnectionStateT]):
    """
    Implements the WebSocket protocol for handling voice connections.

    Opcodes:
    IDENTIFY
        (Send only) Start a new voice session.
    SELECT_PROTOCOL
        (Send only) Tells discord what encryption mode and how to connect for voice.
    READY
        (Receive only) Tells the client that the WebSocket handshake has completed.
    HEARTBEAT
        (Send/Receive) Keeps your WebSocket connection alive.
    SESSION_DESCRIPTION
        (Receive only) Gives you info about current session contains the 'secret_key' to use
    SPEAKING
        (Send/Receive) Indicate which users are speaking.
    HEARTBEAT_ACK
        (Receive only) Tells the client that heartbeat has been acknowledged
    RESUME
        (Send only) Tells the client to resume a previous session that was disconnected
    HELLO
        (Receive only) Sent immediately after connecting, contains the `heartbeat_interval` to use.
    RESUMED
        (Receive only) Tells the client that your RESUME request has succeeded.
    CLIENT_DISCONNECT
        (Receive only) A client has disconnected from the voice channel.
    """

    IDENTIFY = 0
    SELECT_PROTOCOL = 1
    READY = 2
    HEARTBEAT = 3
    SESSION_DESCRIPTION = 4
    SPEAKING = 5
    HEARTBEAT_ACK = 6
    RESUME = 7
    HELLO = 8
    RESUMED = 9
    CLIENT_DISCONNECT = 13

    def __init__(
            self,
            *,
            state: _voiceConnectionStateT,
            web_socket: ClientWebSocketResponse,
            loop: asyncio.AbstractEventLoop,
            hook: Optional['WebsocketHook'] = None) -> None:
        self._state: _voiceConnectionStateT = state
        self._loop: asyncio.AbstractEventLoop = loop
        self._hook: Optional['WebsocketHook'] = hook
        self._web_socket: ClientWebSocketResponse = web_socket

        # State
        self._close_code: Optional[int] = None
        self._is_ready: bool = False

        # Session
        self._keep_alive_handler: Optional[VoiceKeepAliveHandler] = None
        self._ssrc_map: Dict[str, Dict[str, Any]] = {}

    @property
    def state(self) -> _voiceConnectionStateT:
        return self._state

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    @property
    def close_code(self) -> Optional[int]:
        return self._close_code

    @property
    def is_closed(self) -> bool:
        return self._close_code is not None

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    @property
    def latency(self) -> float:
        """
        Latency between a HEARTBEAT and its HEARTBEAT_ACK in seconds.
        """

        heartbeat = self._keep_alive_handler
        return float("inf") if heartbeat is None else heartbeat.latency

    @property
    def average_latency(self) -> list[float] | float:
        """
        Average of last 20 HEARTBEAT latencies.
        """

        heartbeat = self._keep_alive_handler
        if heartbeat is None or not heartbeat.recent_ack_latencies:
            return float("inf")

        return sum(heartbeat.recent_ack_latencies) / len(heartbeat.recent_ack_latencies)

    @abstractmethod
    async def send_resume(self) -> None:
        ...

    @abstractmethod
    async def send_identify(self) -> None:
        ...

    @abstractmethod
    async def send_speak(self, state: Union[SpeakingState, bool] = SpeakingState.voice) -> None:
        ...

    @abstractmethod
    async def send_select_protocol(self, ip: str, port: int, mode: 'SupportedModes') -> None:
        ...

    @abstractmethod
    async def process_received_message(self, data: dict) -> None:
        ...

    async def send_as_json(self, data: Dict[str, Any]) -> None:
        _logger.debug("Sending voice websocket frame: %s.", data)
        await self._web_socket.send_str(_to_json(data))

    async def send_heartbeat(self, data: Dict[str, Any]) -> None:
        await self.send_as_json(data)

    async def poll_event(self) -> None:
        received_message = await asyncio.wait_for(self._web_socket.receive(), timeout=30.0)

        if received_message.type is aiohttp.WSMsgType.TEXT:
            await self.process_received_message(_from_json(received_message.data))

        elif received_message.type is aiohttp.WSMsgType.ERROR:
            _logger.debug(f"Received websocket error: {received_message}")
            raise ConnectionClosed(self._web_socket, shard_id=None) from received_message.data

        elif received_message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
            _logger.debug(f"Received websocket close: {received_message}")
            raise ConnectionClosed(self._web_socket, shard_id=None, code=self._close_code)

    async def close(self, code: int = 1000) -> None:
        if self._keep_alive_handler is not None:
            self._keep_alive_handler.stop()

        self._close_code = code
        await self._web_socket.close(code=code)


# Types
_voiceWST = TypeVar('_voiceWST', bound=BaseCustomVoiceWebSocket)
WebsocketHook: TypeAlias = Callable[[_voiceWST, int, Dict[str, Any]], Coroutine[Any, Any, Any]]
