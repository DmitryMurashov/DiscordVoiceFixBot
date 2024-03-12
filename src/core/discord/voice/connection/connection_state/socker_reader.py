import logging
import threading
from typing import List, Callable, Any

import select

from src.core.discord.voice.connection.base import BaseCustomVoiceConnectionState

SocketReaderCallback = Callable[[bytes], Any]
_logger = logging.getLogger(f"custom_voice.{__name__}")


class SocketReader(threading.Thread):
    def __init__(self, state: BaseCustomVoiceConnectionState) -> None:
        super().__init__(daemon=True, name=f'voice-socket-reader:{id(self):#x}')

        self._state: BaseCustomVoiceConnectionState = state

        self._callbacks: List[SocketReaderCallback] = []
        self._running = threading.Event()
        self._end = threading.Event()

        # If we have paused reading due to having no callbacks
        self._idle_paused: bool = True

    def register(self, callback: SocketReaderCallback) -> None:
        self._callbacks.append(callback)

        if self._idle_paused:
            self._idle_paused = False
            self._running.set()

    def unregister(self, callback: SocketReaderCallback) -> None:
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass
        else:
            if not self._callbacks and self._running.is_set():
                # If running is not set, we are either explicitly paused and
                # should be explicitly resumed, or we are already idle paused

                self._idle_paused = True
                self._running.clear()

    def pause(self) -> None:
        self._idle_paused = False
        self._running.clear()

    def resume(self, *, force: bool = False) -> None:
        if self._running.is_set():
            return

        # Don't resume if there are no callbacks registered
        if not force and not self._callbacks:
            # We tried to resume, but there was nothing to do, so resume when ready
            self._idle_paused = True
            return

        self._idle_paused = False
        self._running.set()

    def stop(self) -> None:
        self._end.set()
        self._running.set()

    def run(self) -> None:
        self._end.clear()
        self._running.set()

        try:
            self._do_run()
        except Exception:
            _logger.exception('Error in %s', self)
        finally:
            self.stop()
            self._running.clear()
            self._callbacks.clear()

    def _do_run(self) -> None:
        while not self._end.is_set():
            if not self._running.is_set():
                self._running.wait()
                continue

            # Since this socket is a non-blocking socket, select has to be used to wait on it for reading.
            try:
                readable, _, _ = select.select([self._state.voice_socket], [], [], 30)
            except (ValueError, TypeError):
                # The socket is either closed or doesn't exist at the moment
                continue

            if not readable:
                continue

            try:
                data = self._state.voice_socket.recv(2048)  # type: ignore
            except OSError:
                _logger.debug(f"Error reading from socket in {self}, this should be safe to ignore")
            else:
                for cb in self._callbacks:
                    try:
                        cb(data)
                    except Exception:
                        _logger.error(f"Error calling callback {cb} in {self}")
