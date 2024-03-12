import logging
from typing import TYPE_CHECKING, Union

from src.core.discord.voice.connection.base import BaseCustomVoiceClient
from src.core.discord.voice.connection.connection_state import CustomVoiceConnectionState
from .player_mixin import CustomVoiceClientThreadPlayerMixin

if TYPE_CHECKING:
    from discord.channel import StageChannel, VoiceChannel

    VocalGuildChannel = Union[VoiceChannel, StageChannel]

_logger = logging.getLogger(f"custom_voice.{__name__}")


class CustomVoiceClient(CustomVoiceClientThreadPlayerMixin, BaseCustomVoiceClient):
    def create_connection_state(self) -> CustomVoiceConnectionState:
        return CustomVoiceConnectionState(self)
