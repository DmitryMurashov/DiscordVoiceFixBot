import logging
from abc import ABC
from typing import Optional

import discord
from discord.player import AudioSource

from src.core.discord.voice.connection.audio_player import CustomThreadAudioPlayer
from src.core.discord.voice.connection.base import BaseCustomVoiceClient, ThreadAudioPlayerCallback

_logger = logging.getLogger(f"custom_voice.{__name__}")


class CustomVoiceClientThreadPlayerMixin(BaseCustomVoiceClient, ABC):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self._player: Optional[CustomThreadAudioPlayer] = None

    @property
    def audio_source(self) -> Optional[AudioSource]:
        return self._player.source if self._player else None

    @audio_source.setter
    def audio_source(self, value: AudioSource) -> None:
        if not isinstance(value, AudioSource):
            raise TypeError(f'expected AudioSource not {value.__class__.__name__}.')
        elif self._player is None:
            raise ValueError('Not playing anything.')

        self._player.set_source(value)

    def is_playing(self) -> bool:
        return self._player is not None and self._player.is_playing()

    def is_paused(self) -> bool:
        return self._player is not None and self._player.is_paused()

    def _prepare_to_play(self, source: discord.AudioSource) -> None:
        if not self.is_connected():
            raise discord.ClientException('Not connected to voice.')
        elif self.is_playing():
            raise discord.ClientException('Already playing audio.')
        elif not isinstance(source, discord.AudioSource):
            raise TypeError(f'source must be an AudioSource not {source.__class__.__name__}')

    def _create_player(self, source: discord.AudioSource, *, callback: Optional[ThreadAudioPlayerCallback] = None) -> CustomThreadAudioPlayer:
        return CustomThreadAudioPlayer(
            source=source,
            voice_client=self,
            after=callback
        )

    def play(self, source: discord.AudioSource, *, after: Optional[ThreadAudioPlayerCallback] = None) -> None:
        self._prepare_to_play(source)

        self._player = self._create_player(source=source, callback=after)
        self._player.start()

    async def play_async(self, source: discord.AudioSource) -> None:
        self.play(source)
        await self._player.async_wait_until_ended()  # type: ignore

    def stop(self) -> None:
        if self._player is None:
            return

        self._player.stop()
        self._player = None

    def pause(self) -> None:
        if self._player is None:
            return

        self._player.pause()

    def resume(self) -> None:
        if self._player is None:
            return

        self._player.resume()

    async def disconnect(self, *, force: bool = False) -> None:
        self.stop()
        await super().disconnect(force=force)
