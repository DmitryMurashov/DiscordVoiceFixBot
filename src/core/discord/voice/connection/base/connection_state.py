import asyncio
import socket
from abc import abstractmethod
from typing import TYPE_CHECKING, Tuple, Optional, List

import discord
from discord.abc import Snowflake
from discord.member import VoiceState

from src.core.discord.voice.connection.voice_web_socket import CustomVoiceWebSocket
from .voice_web_socket import WebsocketHook

if TYPE_CHECKING:
    from discord.types.voice import (
        SupportedModes,
        GuildVoiceState as GuildVoiceStatePayload,
        VoiceServerUpdate as VoiceServerUpdatePayload
    )

    from .voice_client import BaseCustomVoiceClient, VocalGuildChannel


class BaseCustomVoiceConnectionState:
    def __init__(
            self,
            voice_client: 'BaseCustomVoiceClient',
            default_timeout: float = 15.0,
            *,
            hook: Optional[WebsocketHook] = None) -> None:
        self._voice_client = voice_client
        self._hook = hook

        # Parameters
        self.timeout: float = default_timeout
        self.reconnect = True

        # Connection info (Used for getting UDP server data in websocket)
        self.token: Optional[str] = None
        self.server_id: Optional[int] = None

        # Session (Used for WebSocket and UDP socket)
        self.session_id: Optional[str] = None  # Session id. Fetched in websocket handshake
        self.secret_key: Optional[List[int]] = None  # Session secret key. Fetched in websocket handshake

        # Websocket data
        self.ssrc: Optional[int] = None  # Used in websocket
        self.endpoint: Optional[str] = None  # Websocket endpoint. Used for connecting websocket

        # UDP socket data
        self.ip: Optional[str] = None  # UDP socket ip
        self.port: Optional[int] = None  # UDP socket port
        self.mode: Optional[SupportedModes] = None  # Encoding mode for UDP socket

        # Sockets
        self.socket: Optional[socket.socket] = None  # Used for getting voice data (audio)
        self.ws: Optional[CustomVoiceWebSocket] = None  # User for creating UDP connection and getting events

    @property
    def guild(self) -> discord.Guild:
        return self._voice_client.guild

    @property
    def channel(self) -> 'VocalGuildChannel':
        return self._voice_client.channel

    @channel.setter
    def channel(self, value: 'VocalGuildChannel') -> None:
        self._voice_client.channel = value

    @property
    def user(self) -> discord.ClientUser:
        return self._voice_client.user

    @property
    def supported_modes(self) -> Tuple['SupportedModes', ...]:
        return self._voice_client.supported_modes

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._voice_client.loop

    @property
    def self_voice_state(self) -> Optional[VoiceState]:
        return self.guild.me.voice

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    async def on_voice_state_update(self, data: 'GuildVoiceStatePayload') -> None: ...

    @abstractmethod
    async def on_voice_server_update(self, data: 'VoiceServerUpdatePayload') -> None: ...

    @abstractmethod
    async def connect(self, *, reconnect: bool, timeout: float, self_deaf: bool, self_mute: bool, resume: bool, wait: bool = True) -> None: ...

    @abstractmethod
    async def disconnect(self, *, force: bool = True, cleanup: bool = True) -> None: ...

    @abstractmethod
    async def move_to(self, channel: Optional[Snowflake], timeout: Optional[float] = 30.0) -> None: ...

    @abstractmethod
    async def async_wait_until_connected(self, timeout: Optional[float] = 30) -> bool: ...

    @abstractmethod
    def wait_until_connected(self, timeout: Optional[float] = 30.0) -> bool: ...

    @abstractmethod
    def send_packet(self, packet: bytes) -> None: ...
