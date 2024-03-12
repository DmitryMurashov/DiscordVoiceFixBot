import asyncio
import logging
import threading
from typing import TypeVar, Generic, TypeAlias, Callable, Optional, Any

import discord

from src.core.discord.voice.connection.base import BaseCustomVoiceClient
from src.core.utils.errors import get_traceback_text
from abc import ABC, abstractmethod

_logger = logging.getLogger(f"custom_voice.{__name__}")

_voiceClientT = TypeVar("_voiceClientT", bound=BaseCustomVoiceClient)
_audioSourceT = TypeVar("_audioSourceT", bound=discord.AudioSource)
ThreadAudioPlayerCallback: TypeAlias = Callable[[Optional[Exception]], Any]


class BaseCustomThreadAudioPlayer(threading.Thread, ABC, Generic[_voiceClientT, _audioSourceT]):
    def __init__(
            self,
            source: _audioSourceT,
            voice_client: _voiceClientT,
            *,
            reconnect_timeout: float = 15.0,
            after: Optional[ThreadAudioPlayerCallback] = None) -> None:
        super().__init__(daemon=True)

        self._voice_client: _voiceClientT = voice_client
        self._source: _audioSourceT = source
        self._callback: Optional[ThreadAudioPlayerCallback] = after
        self._loop: asyncio.AbstractEventLoop = self._voice_client.loop
        self._reconnect_timeout = reconnect_timeout

        # State
        self._current_error: Optional[Exception] = None
        self._lock: threading.Lock = threading.Lock()

    @property
    def voice_client(self) -> _voiceClientT:
        return self._voice_client

    @property
    def source(self) -> _audioSourceT:
        return self._source

    @property
    def current_error(self) -> Optional[Exception]:
        return self._current_error

    @abstractmethod
    def is_playing(self) -> bool:
        ...

    @abstractmethod
    def is_paused(self) -> bool:
        ...

    @abstractmethod
    def play(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @abstractmethod
    def pause(self, *, update_speaking: bool = True) -> None:
        ...

    @abstractmethod
    def resume(self, *, update_speaking: bool = True) -> None:
        ...

    @abstractmethod
    async def async_wait_until_ended(self, timeout: Optional[float] = None) -> None:
        ...

    def _call_callback(self) -> None:
        if self._callback is not None:
            try:
                self._callback(self._current_error)
            except Exception as error:
                _logger.error(f"Callback function call ended with error:\n{get_traceback_text(error)}")
        elif self._current_error:
            _logger.error(f"Unexpected exception in player thread {self.name}:\n{get_traceback_text(self._current_error)}")

    def _get_data(self) -> Optional[bytes]:
        return self._source.read()

    def _send_data(self, data: bytes) -> None:
        self._voice_client.send_audio_packet(data, encode=not self._source.is_opus())

    def _set_speak(self, speaking: bool) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._voice_client.ws.send_speak(speaking), self._loop)
        except Exception as e:
            _logger.info("Speaking call in player failed: %s", e)

    def set_source(self, source: _audioSourceT) -> None:
        with self._lock:
            self.pause(update_speaking=False)
            self._source = source
            self.resume(update_speaking=False)

    def run(self) -> None:
        try:
            self.play()
        except Exception as error:
            self._current_error = error
            self.stop()
        finally:
            self.source.cleanup()
            self._call_callback()
