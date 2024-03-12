import asyncio
import socket
from abc import abstractmethod
from typing import TYPE_CHECKING, Tuple, Optional, List, TypeVar

import discord
from discord.abc import Snowflake
from discord.member import VoiceState

from .voice_web_socket import BaseCustomVoiceWebSocket, WebsocketHook

if TYPE_CHECKING:
    from discord.types.voice import (
        SupportedModes,
        GuildVoiceState as GuildVoiceStatePayload,
        VoiceServerUpdate as VoiceServerUpdatePayload
    )

    from .voice_client import BaseCustomVoiceClient, VocalGuildChannel  # type: ignore

_voiceClientT = TypeVar("_voiceClientT", bound='BaseCustomVoiceClient')


class BaseCustomVoiceConnectionState:
    def __init__(
            self,
            voice_client: _voiceClientT,
            *,
            timeout: float = 15.0,
            websocket_hook: Optional[WebsocketHook] = None) -> None:
        self._voice_client: _voiceClientT = voice_client
        self._websocket_hook = websocket_hook

        # Parameters
        self.timeout: float = timeout
        self.reconnect = True

        # Websocket
        self.websocket_endpoint: Optional[str] = None  # Used for connecting websocket

        # Connection info (Fetched during voice state/server update. Used for connecting websocket)
        self.token: Optional[str] = None
        self.server_id: Optional[int] = None
        self.session_id: Optional[str] = None

        # Session info (Fetched during websocket handshake. Used for sending voice data)
        self.secret_key: Optional[List[int]] = None
        self.ssrc: Optional[int] = None

        # Voice server info (Fetched during websocket handshake. Used for sending voice data)
        self.voice_server_ip: Optional[str] = None
        self.voice_server_port: Optional[int] = None
        self.voice_encryption_mode: Optional[SupportedModes] = None

        # Sockets
        self.voice_socket: Optional[socket.socket] = None  # Used for getting/sending voice data (audio)
        self.ws: Optional[BaseCustomVoiceWebSocket] = None  # Used for creating UDP connection and getting events

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
    def is_reconnecting(self) -> bool: ...

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
    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None: ...

    def set_voice_server_protocol(self, ip: str, port: int) -> None:
        self.voice_server_ip = ip
        self.voice_server_port = port

    def set_voice_socket_encryption_mode(self, mode: 'SupportedModes') -> None:
        self.voice_encryption_mode = mode
