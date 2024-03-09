import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Union, TypeVar, Optional, List, Generic, TypeAlias, Tuple

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
_connectionStateT = TypeVar("_connectionStateT", bound=BaseCustomVoiceConnectionState)


class BaseCustomVoiceClient(discord.VoiceProtocol, ABC, Generic[_connectionStateT]):
    supported_modes: Tuple['SupportedModes', ...] = (
        'xsalsa20_poly1305_lite',
        'xsalsa20_poly1305_suffix',
        'xsalsa20_poly1305',
    )

    warn_nacl: bool = False
    channel: 'VocalGuildChannel'

    def __init__(self, client: Client, channel: 'VocalGuildChannel') -> None:
        super().__init__(client, channel)

        state = client._connection

        self.loop: asyncio.AbstractEventLoop = state.loop
        self._state: ConnectionState = state
        self._connection: _connectionStateT = self.create_connection_state()
        self._connected = self._connection._connected  # type: ignore
        self.guild = self.channel.guild

    @property
    def connection(self) -> '_connectionStateT':
        return self._connection

    @property
    def user(self) -> discord.ClientUser:
        return self._state.user  # type: ignore

    @property
    def session_id(self) -> Optional[str]:
        return self._connection.session_id

    @property
    def token(self) -> Optional[str]:
        return self._connection.token

    @property
    def endpoint(self) -> Optional[str]:
        return self._connection.endpoint

    @property
    def secret_key(self) -> Optional[List[int]]:
        return self._connection.secret_key

    @property
    def ws(self) -> Optional[BaseCustomVoiceWebSocket]:
        return self._connection.ws

    @property
    def timeout(self) -> float:
        return self._connection.timeout

    @abstractmethod
    def create_connection_state(self) -> _connectionStateT:
        ...

    def is_connected(self) -> bool:
        return self._connection.is_connected()

    async def on_voice_state_update(self, data: 'GuildVoiceStatePayload') -> None:
        await self._connection.on_voice_state_update(data)

    async def on_voice_server_update(self, data: 'VoiceServerUpdatePayload') -> None:
        await self._connection.on_voice_server_update(data)

    async def connect(self, *, reconnect: bool, timeout: float, self_deaf: bool = False, self_mute: bool = False) -> None:
        await self._connection.connect(
            reconnect=reconnect,
            timeout=timeout,
            self_deaf=self_deaf,
            self_mute=self_mute,
            resume=False
        )

    async def disconnect(self, *, force: bool = False) -> None:
        try:
            await self._connection.disconnect(force=force)
        finally:
            self.cleanup()

    async def move_to(self, channel: Optional[Snowflake], *, timeout: Optional[float] = 30.0) -> None:
        await self._connection.move_to(channel, timeout)

    def wait_until_connected(self, timeout: Optional[float] = 30.0) -> bool:
        return self._connection.wait_until_connected(timeout=timeout)

    async def async_wait_until_connected(self, timeout: Optional[float] = 30) -> bool:
        return await self._connection.async_wait_until_connected(timeout=timeout)
