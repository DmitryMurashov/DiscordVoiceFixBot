import asyncio
import logging
import threading
import time
from typing import Optional

from discord.opus import Encoder as DiscordOpusEncoder

from src.core.discord.voice.connection.base import BaseCustomThreadAudioPlayer
from src.core.discord.voice.connection.exceptions import (
    TemporaryDisconnectedFromVoiceError,
    DisconnectedFromVoiceError
)
from src.core.utils.async_tools import SyncAndAsyncEvent

_logger = logging.getLogger(f"custom_voice.{__name__}")


class CustomThreadAudioPlayer(BaseCustomThreadAudioPlayer):
    DELAY: float = DiscordOpusEncoder.FRAME_LENGTH / 1000.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        # State
        self._end: SyncAndAsyncEvent = SyncAndAsyncEvent()
        self._resumed: threading.Event = threading.Event()
        self._resumed.set()

        self._loops: int = 0
        self._start: int = 0

    def _cleanup(self) -> None:
        self._loops = 0
        self._start = time.perf_counter()

    def is_playing(self) -> bool:
        return not self._end.is_set() and self._resumed.is_set() and self.is_alive()

    def is_paused(self) -> bool:
        return not self._end.is_set() and not self._resumed.is_set() and self.is_alive()

    def stop(self) -> None:
        self._end.set()
        self._resumed.set()
        self._set_speak(False)

    def pause(self, *, update_speaking: bool = True) -> None:
        self._resumed.clear()

        if update_speaking:
            self._set_speak(False)

    def resume(self, *, update_speaking: bool = True) -> None:
        self._cleanup()
        self._resumed.set()

        if update_speaking:
            self._set_speak(True)

    async def async_wait_until_ended(self, timeout: Optional[float] = None) -> None:
        if not (await self._end.wait_async(timeout=timeout)):
            raise asyncio.TimeoutError()

    def _reconnect(self) -> bool:
        if not self._voice_client.is_reconnecting():
            _logger.debug("Disconnected from voice. Stopping player")
            return False

        _logger.debug("Reconnecting to voice channel. Player is waiting for connection")
        return self._voice_client.wait_until_reconnected(timeout=self._reconnect_timeout)

    def play(self) -> None:
        self._cleanup()
        self._set_speak(True)

        while not self._end.is_set():
            if not self._resumed.is_set():
                self._resumed.wait()
                continue

            if not self._voice_client.is_connected():
                reconnected = self._reconnect()

                if not reconnected:
                    _logger.debug("Reconnect unsuccessful. Stopping player")
                    self.stop()
                    return

                _logger.debug("Reconnected to voice. Continue playing")
                self._cleanup()
                self._set_speak(True)
                continue

            self._loops += 1

            if not (data := self._get_data()):
                self.stop()
                return

            try:
                self._send_data(data)
            except TemporaryDisconnectedFromVoiceError as error:
                _logger.warning(f"Unable to send audio packet (reconnecting): {error.__class__.__name__}: {error}")
                continue
            except DisconnectedFromVoiceError as error:
                _logger.warning(f"Unable to send audio packet (disconnected): {error.__class__.__name__}: {error}")
                self.stop()
                return

            next_time = self._start + self.DELAY * self._loops
            if (delay := self.DELAY + (next_time - time.perf_counter())) > 0:
                time.sleep(delay)
