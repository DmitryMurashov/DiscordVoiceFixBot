import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Union, TypeVar, Optional, Generic, TypeAlias, Tuple

import discord
from discord.abc import Snowflake
from discord.channel import StageChannel, VoiceChannel
from discord.client import Client
from discord.state import ConnectionState

from .connection_state import BaseCustomVoiceConnectionState
from .voice_web_socket import BaseCustomVoiceWebSocket

if TYPE_CHECKING:
    from discord.types.voice import (
        SupportedModes,
        GuildVoiceState as GuildVoiceStatePayload,
        VoiceServerUpdate as VoiceServerUpdatePayload
    )

# Type aliases
VocalGuildChannel: TypeAlias = Union[VoiceChannel, StageChannel]
_voiceConnectionStateT = TypeVar("_voiceConnectionStateT", bound=BaseCustomVoiceConnectionState)


class BaseCustomVoiceClient(discord.VoiceProtocol, ABC, Generic[_voiceConnectionStateT]):
    supported_modes: Tuple['SupportedModes', ...] = (
        'xsalsa20_poly1305_lite',
        'xsalsa20_poly1305_suffix',
        'xsalsa20_poly1305',
    )

    warn_nacl: bool = False
    channel: 'VocalGuildChannel'

    def __init__(self, client: Client, channel: 'VocalGuildChannel') -> None:
        super().__init__(client, channel)

        self._state: ConnectionState = client._connection
        self._voice_connection_state: _voiceConnectionStateT = self.create_connection_state()

        self.loop: asyncio.AbstractEventLoop = self._state.loop
        self.guild = self.channel.guild

    @property
    def timeout(self) -> float:
        return self._voice_connection_state.timeout

    @property
    def user(self) -> discord.ClientUser:
        return self._state.user  # type: ignore

    @property
    def ws(self) -> Optional[BaseCustomVoiceWebSocket]:
        return self._voice_connection_state.ws

    @property
    def connection(self) -> _voiceConnectionStateT:
        return self._voice_connection_state

    @abstractmethod
    def create_connection_state(self) -> _voiceConnectionStateT:
        ...

    def is_connected(self) -> bool:
        return self._voice_connection_state.is_connected()

    def is_reconnecting(self) -> bool:
        return self._voice_connection_state.is_reconnecting()

    async def on_voice_state_update(self, data: 'GuildVoiceStatePayload') -> None:
        await self._voice_connection_state.on_voice_state_update(data)

    async def on_voice_server_update(self, data: 'VoiceServerUpdatePayload') -> None:
        await self._voice_connection_state.on_voice_server_update(data)

    async def connect(self, *, reconnect: bool, timeout: float, self_deaf: bool = False, self_mute: bool = False) -> None:
        await self._voice_connection_state.connect(
            reconnect=reconnect,
            timeout=timeout,
            self_deaf=self_deaf,
            self_mute=self_mute,
            resume=False
        )

    async def disconnect(self, *, force: bool = False) -> None:
        try:
            await self._voice_connection_state.disconnect(force=force)
        finally:
            self.cleanup()

    async def move_to(self, channel: Optional[Snowflake], *, timeout: Optional[float] = 30.0) -> None:
        await self._voice_connection_state.move_to(channel, timeout)

    def wait_until_connected(self, timeout: Optional[float] = 30.0) -> bool:
        return self._voice_connection_state.wait_until_connected(timeout=timeout)

    async def async_wait_until_connected(self, timeout: Optional[float] = 30.0) -> bool:
        return await self._voice_connection_state.async_wait_until_connected(timeout=timeout)

    async def async_wait_until_reconnected(self, timeout: Optional[float] = 30.0) -> bool:
        return await self._voice_connection_state.async_wait_until_reconnected(timeout=timeout)

    def wait_until_reconnected(self, timeout: Optional[float] = 30.0) -> bool:
        return self._voice_connection_state.wait_until_reconnected(timeout=timeout)

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        self._voice_connection_state.send_audio_packet(data, encode=encode)
