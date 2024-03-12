import asyncio
from concurrent.futures import CancelledError as FutureCancelled
import logging
import socket
import threading
from typing import Optional, Callable

import discord
from discord.abc import Snowflake
from discord.backoff import ExponentialBackoff
from discord.types.voice import (
    GuildVoiceState as GuildVoiceStatePayload,
    VoiceServerUpdate as VoiceServerUpdatePayload
)

from src.core.discord.voice.connection.base import BaseCustomVoiceConnectionState
from src.core.discord.voice.connection.exceptions import DisconnectedFromVoiceError, TemporaryDisconnectedFromVoiceError
from src.core.discord.voice.connection.utils.audio_encryption import AudioEncoder
from src.core.discord.voice.connection.voice_web_socket import CustomVoiceWebSocket
from src.core.utils.async_tools import AsyncEventProvider, SyncAndAsyncEvent
from src.core.utils.errors import get_traceback_text
from .socker_reader import SocketReader

_logger = logging.getLogger(f"custom_voice.{__name__}")


class CustomVoiceConnectionState(BaseCustomVoiceConnectionState):
    _VOICE_STATE_UPDATED_EVENT: str = "voice_state_updated"
    _VOICE_SERVER_UPDATED_EVENT: str = "voice_server_updated"
    _CONNECTED_EVENT: str = "connected"
    _DISCONNECTED_EVENT: str = "disconnected"

    _MAX_CONNECTION_ATTEMPTS: int = 5

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        # State
        self._event_provider: AsyncEventProvider = AsyncEventProvider()
        self._connected: SyncAndAsyncEvent = SyncAndAsyncEvent()
        self._expecting_disconnect: bool = False

        # Tasks
        self._connector_task: Optional[asyncio.Task] = None
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._potential_reconnect_task: Optional[asyncio.Task] = None

        # Audio encoder
        self._audio_encoder: AudioEncoder = AudioEncoder()

        # Socker reader
        self._socket_reader = SocketReader(self)
        self._socket_reader.start()

    def _parse_endpoint(self, endpoint: str) -> str:
        endpoint, _, _ = endpoint.rpartition(":")
        if endpoint.startswith("wss://"):
            # Just in case, strip it off since we're going to add it later
            endpoint = endpoint[6:]

        return endpoint

    def _update_voice_channel(self, channel_id: Optional[int]) -> None:
        self.channel = channel_id and self.guild.get_channel(channel_id)  # type: ignore

    def _stop_connector(self) -> None:
        if self._connector_task is None:
            return

        if not self._connector_task.done():
            _logger.info("Stopping connector")

            self._connector_task.cancel()

        _logger.debug("Connector removed")
        self._connector_task = None

    def _stop_dispatcher(self) -> None:
        if self._dispatcher_task is None:
            return

        if not self._dispatcher_task.done():
            _logger.info("Stopping dispatcher")

            self._dispatcher_task.cancel()

        _logger.debug("Dispatcher removed")
        self._dispatcher_task = None

    async def _change_voice_state(self, **kwargs) -> None:
        _logger.debug(f"Changing voice state: {kwargs}")

        await self.guild.change_voice_state(**kwargs)

    async def _send_change_channel(self, channel: Snowflake) -> None:
        _logger.debug(f"Changing channel to {channel.id}")

        await self._change_voice_state(channel=channel)

    async def _send_voice_connect(self, self_deaf: bool = False, self_mute: bool = False) -> None:
        _logger.debug("Sending voice connect")

        await self._change_voice_state(channel=self.channel, self_mute=self_mute, self_deaf=self_deaf)

    async def _send_voice_disconnect(self) -> None:
        _logger.debug("Sending voice disconnect")

        self._expecting_disconnect = True
        await self._change_voice_state(channel=None)

    async def _create_voice_socket(self) -> None:
        _logger.debug("Creating voice socket")

        if self.voice_socket is not None:
            self.voice_socket.close()

        self.voice_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.voice_socket.setblocking(False)
        self._socket_reader.resume()

    async def _create_websocket(self, *, resume: bool = False) -> CustomVoiceWebSocket:
        _logger.debug("Creating websocket")

        if self.ws is not None and self.ws.close_code is None:
            _logger.debug("Closing old websocket with code 4000")
            await self.ws.close(code=4000)

        gateway = f'wss://{self.websocket_endpoint}/?v=4'
        http = self._voice_client._state.http

        sock = await http.ws_connect(gateway, compress=15)
        ws = CustomVoiceWebSocket(
            state=self,
            web_socket=sock,
            loop=self._voice_client.loop,
            hook=self._websocket_hook
        )

        ws._max_heartbeat_timeout = 60.0  # type: ignore
        ws.thread_id = threading.get_ident()  # type: ignore

        if resume:
            await ws.send_resume()
        else:
            await ws.send_identify()

        return ws

    async def _connect_websocket(self, resume: bool = False) -> None:
        # Stop the websocket reader because closing the websocket will trigger an unwanted reconnect/disconnect
        if self._dispatcher_task is not None:
            self._stop_dispatcher()

        _logger.debug("Connecting websocket")
        self.ws = await self._create_websocket(resume=resume)
        await self._handshake_websocket(self.ws)

        _logger.info("Creating dispatcher task")
        self._dispatcher_task = self.loop.create_task(self._poll_voice_ws())

    async def _close_connection(self, close_code: int = 1000, cleanup: bool = False) -> None:
        _logger.info('Closing connection')

        # Stop the websocket reader because closing the websocket will trigger an unwanted reconnect
        if self._dispatcher_task is not None:
            self._stop_dispatcher()

        try:
            if self.ws is not None and self.ws.close_code is None:
                await self.ws.close(code=close_code)
        except Exception as error:
            _logger.debug(f"Ignoring exception in close_connection_safe: {error.__class__.__name__}: {error}")
        finally:
            self.voice_server_ip = None
            self.voice_server_port = None
            self._socket_reader.pause()

            # Flip the connected event to unlock any waiters
            self._connected.set()
            self._connected.clear()
            self._event_provider.dispatch_event(self._DISCONNECTED_EVENT)

            if self.voice_socket is not None:
                self.voice_socket.close()

            if cleanup:
                self._socket_reader.stop()
                self._voice_client.cleanup()

            _logger.info('Connection closed')

    async def _handshake_websocket(self, ws: CustomVoiceWebSocket) -> None:
        _logger.debug("Handshaking websocket")

        async with asyncio.timeout(self.timeout):
            while not ws.is_ready:
                await ws.poll_event()

        _logger.debug("Websocket handshake successful")

    async def _handshake(self, self_deaf: bool, self_mute: bool) -> None:
        await self._send_voice_connect(self_deaf=self_deaf, self_mute=self_mute)

        _logger.debug("Waiting for updates")

        try:
            await self._event_provider.wait_for_events(
                (self._VOICE_STATE_UPDATED_EVENT, self._VOICE_SERVER_UPDATED_EVENT),
                timeout=self.timeout
            )
        except asyncio.TimeoutError as error:
            _logger.debug("Timeout waiting for updates. Closing connection")
            await self.disconnect(force=True)
            raise error

        _logger.debug("Updates performed. Connecting...")

    async def _perform_potential_reconnect(self) -> bool:
        attempts = 0

        while True:
            if attempts > self._MAX_CONNECTION_ATTEMPTS:
                _logger.debug(f"Potential reconnection failed: maximum number of attempts reached")
                return False

            try:
                await self._connect_websocket(resume=False)
            except discord.ConnectionClosed as error:
                _logger.debug(f"Connection closed during reconnect: {error.__class__.__name__}: {error}")

                if error.code == 4014:
                    _logger.debug("Got close code 4014; Reconnecting again")
                    continue
                elif error.code == 4006:
                    _logger.debug("Got close code 4006; Reconnecting again in 0.5s")
                    await asyncio.sleep(0.5)
                    attempts += 1
                    continue

                return False
            except OSError as error:
                _logger.debug(
                    f"Potential reconnection failed (Reconnecting in 0.5s): {error.__class__.__name__}: {error}")
                await asyncio.sleep(0.5)

                attempts += 1
                continue
            except asyncio.TimeoutError:
                _logger.debug(f"Potential reconnection failed: timeout")
                return False
            else:
                _logger.debug("Potential reconnection successful")
                self._connected.set()
                self._event_provider.dispatch_event(self._CONNECTED_EVENT)

                return True

    async def _potential_reconnect(self) -> bool:
        _logger.debug("Potential reconnecting")
        self._connected.clear()
        self._event_provider.dispatch_event(self._DISCONNECTED_EVENT)

        try:
            await self._event_provider.wait_for_event(self._VOICE_SERVER_UPDATED_EVENT, timeout=self.timeout)
        except asyncio.TimeoutError:
            return False

        while True:
            _logger.debug("Starting reconnect tasks")
            reconnect_task = asyncio.create_task(self._perform_potential_reconnect())
            server_update_observer_task = asyncio.create_task(
                self._event_provider.wait_for_event(self._VOICE_SERVER_UPDATED_EVENT))

            await asyncio.wait([reconnect_task, server_update_observer_task], return_when=asyncio.FIRST_COMPLETED)

            if server_update_observer_task.done():
                _logger.debug("Got voice server update while reconnecting. Retrying")

                if not reconnect_task.cancelled():
                    _logger.debug("Cancelling reconnect task")
                    reconnect_task.cancel()
                continue
            else:
                if not server_update_observer_task.cancelled():
                    server_update_observer_task.cancel()

                _logger.debug("Reconnect task completed")
                self._potential_reconnect_task = None
                return reconnect_task.result()

    async def _on_voice_state_update(self, data: 'GuildVoiceStatePayload') -> None:
        self.session_id = data["session_id"]
        channel_id = data.get("channel_id", None)

        if channel_id is None:
            if self._expecting_disconnect:
                _logger.debug('As expected, disconnect performed')
                self._expecting_disconnect = False
            else:
                _logger.debug('We were externally disconnected from voice.')
                await self.disconnect(force=True)  # force=False

            return

        channel_id = int(channel_id)
        if channel_id is not None and self.channel.id != channel_id:
            self._update_voice_channel(channel_id)

        self._event_provider.dispatch_event(self._VOICE_STATE_UPDATED_EVENT)

    async def _on_voice_server_update(self, data: 'VoiceServerUpdatePayload') -> None:
        self.token = data.get("token")
        self.server_id = int(data["guild_id"])

        endpoint = data.get("endpoint", None)
        if endpoint is None or self.token is None:
            _logger.warning(
                "Awaiting endpoint... This requires waiting. "
                "If timeout occurred considering raising the timeout and reconnecting."
            )
            return

        self.websocket_endpoint = self._parse_endpoint(endpoint)
        await self._create_voice_socket()
        self._event_provider.dispatch_event(self._VOICE_SERVER_UPDATED_EVENT)

    async def _connector(self, *, reconnect: bool, self_deaf: bool, self_mute: bool, resume: bool) -> None:
        _logger.info("Connector task started")

        for attempt in range(self._MAX_CONNECTION_ATTEMPTS):
            _logger.debug(f"Attempting to connect to voice (attempt {attempt})")

            try:
                await self._handshake(self_deaf=self_deaf, self_mute=self_mute)
                await self._connect_websocket(resume=resume)
                break
            except discord.ConnectionClosed as error:
                if error.code in (4006, 4014):
                    _logger.error(f"Failed to connect to voice: Disconnected during connection (code {error.code})")
                    raise error

                sleep_time = 1 + attempt * 2.0
                _logger.error(f"Failed to connect to voice (Retrying in {sleep_time}): connection closed with code {error.code}")
                await asyncio.sleep(sleep_time)

                await self._send_voice_disconnect()

            except (asyncio.TimeoutError, OSError) as error:
                if not reconnect:
                    raise

                sleep_time = 1 + attempt * 2.0
                _logger.error(f"Failed to connect to voice (Retrying in {sleep_time}): {error.__class__.__name__}: {error}")
                await asyncio.sleep(sleep_time)

                await self._send_voice_disconnect()
                continue

        _logger.debug("Successfully connected to voice")
        self._connected.set()
        self._event_provider.dispatch_event(self._CONNECTED_EVENT)

    async def _wrap_connector(self, func: Callable, *args, **kwargs) -> None:
        try:
            await func(*args, **kwargs)
        except asyncio.CancelledError:
            _logger.debug('Closing voice connection because connector task was cancelled')
            await self.disconnect()
        except asyncio.TimeoutError:
            _logger.info('Timed out connecting to voice. Disconnecting normally')
            await self.disconnect()
        except Exception as error:
            _logger.error(f'Got unknown error connecting to voice:\n{get_traceback_text(error)}')
            await self.disconnect()
            raise error

    async def _connect(self, self_deaf: bool, self_mute: bool, resume: bool, wait: bool = True) -> None:
        _logger.info(f"Connecting to the voice channel {self.channel.id}")

        if self._connector_task is not None:
            self._stop_connector()

        self._connector_task = self.loop.create_task(
            self._wrap_connector(
                self._connector,
                reconnect=self.reconnect,
                self_deaf=self_deaf,
                self_mute=self_mute,
                resume=resume
            )
        )

        # Just to set self._connector_task = None after connecting is finished
        self._connector_task.add_done_callback(lambda _: self._stop_connector())

        if wait:
            _logger.debug("Waiting for connector task to end")
            await self._connector_task

    async def _poll_voice_ws(self, reconnect: bool = True) -> None:
        backoff = ExponentialBackoff()

        while True:
            try:
                if self.ws is None or self.ws.is_closed:
                    self._stop_dispatcher()
                    break

                await self.ws.poll_event()
            except asyncio.CancelledError:
                return
            except (discord.ConnectionClosed, asyncio.TimeoutError) as error:
                # The following close codes are undocumented, so I will document them here.
                # 1000 - normal closure
                # 4014 - we were externally disconnected (a voice channel deleted, we were moved, etc.)
                # 4015 - voice server has crashed

                if isinstance(error, discord.ConnectionClosed):
                    if error.code in (1000, 4015):
                        _logger.info(f'Disconnecting from voice normally, close code {error.code}')
                    elif error.code in (4014, 4006):
                        _logger.info(f'Disconnected from voice by force (code {error.code}); Reconnecting')
                        self._potential_reconnect_task = asyncio.create_task(self._potential_reconnect())
                        break
                    else:
                        _logger.info(f"Unknown close code {error.code} ({error.reason or 'no reason'})'")

                    if not self._expecting_disconnect:
                        await self.disconnect()
                    return

                if not reconnect:
                    await self.disconnect()
                    raise

                sleep_time = backoff.delay()
                _logger.exception("Disconnected from voice... Reconnecting in %.2fs.", sleep_time)

                self._connected.clear()
                self._event_provider.dispatch_event(self._DISCONNECTED_EVENT)

                await asyncio.sleep(sleep_time)
                await self._send_voice_disconnect()

                try:
                    voice_state = self.self_voice_state

                    await self._connect(
                        self_deaf=voice_state.self_deaf if voice_state is not None else False,
                        self_mute=voice_state.self_mute if voice_state is not None else False,
                        resume=False
                    )
                except asyncio.TimeoutError:
                    _logger.warning("Could not connect to voice... Retrying...")
                    continue

    async def on_voice_state_update(self, data: 'GuildVoiceStatePayload') -> None:
        _logger.debug("Got voice state update")
        await self._on_voice_state_update(data)

    async def on_voice_server_update(self, data: 'VoiceServerUpdatePayload') -> None:
        _logger.debug("Got voice server update")
        await self._on_voice_server_update(data)

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def is_reconnecting(self) -> bool:
        return self._potential_reconnect_task is not None and not self._potential_reconnect_task.done()

    async def async_wait_until_connected(self, timeout: Optional[float] = 30) -> bool:
        return await self._connected.wait_async(timeout=timeout)

    def wait_until_connected(self, timeout: Optional[float] = 30.0) -> bool:
        return self._connected.wait(timeout=timeout)

    async def async_wait_until_reconnected(self, timeout: Optional[float] = 30.0) -> bool:
        if not self.is_reconnecting():
            return self.is_connected()

        try:
            return await asyncio.wait_for(self._potential_reconnect_task, timeout=timeout)
        except asyncio.TimeoutError:
            return False

    def wait_until_reconnected(self, timeout: Optional[float] = 30.0) -> bool:
        if not self.is_reconnecting():
            return self.is_connected()

        try:
            return asyncio.run_coroutine_threadsafe(
                asyncio.wait_for(self._potential_reconnect_task, timeout=timeout),
                self.loop
            ).result()
        except FutureCancelled:
            return self.is_connected()
        except asyncio.TimeoutError:
            return False

    async def connect(self, *, reconnect: bool, timeout: float, self_deaf: bool, self_mute: bool, resume: bool, wait: bool = True) -> None:
        if self._dispatcher_task is not None:
            self._stop_dispatcher()

        self.timeout = timeout
        self.reconnect = reconnect

        await self._connect(self_deaf=self_deaf, self_mute=self_mute, resume=resume, wait=wait)

    async def disconnect(self, *, force: bool = True, cleanup: bool = True) -> None:
        if not force and not self.is_connected():
            return

        try:
            await self._send_voice_disconnect()
            await self._close_connection(cleanup=cleanup)
        except Exception as error:
            _logger.debug(f"Ignoring error while disconnecting: {error.__class__.__name__}: {error}")

    async def move_to(self, channel: Optional[Snowflake], timeout: Optional[float] = 30.0) -> None:
        if channel is None:  # Move to None channel = Disconnect
            _logger.debug("Move to None channel. Disconnecting")
            await self.disconnect()
            return

        if self.channel and channel.id == self.channel.id:  # Move to the same channel. Ignoring it
            return

        # Changing the channel and waiting for bot to receive voice_state_update and reconnect
        await self._send_change_channel(channel=channel)
        try:
            await self.async_wait_until_connected()
        except asyncio.TimeoutError:
            _logger.warning('Timed out trying to move to channel %s in guild %s', channel.id, self.guild.id)

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        if self.is_reconnecting():
            raise TemporaryDisconnectedFromVoiceError("Unable to send paket: reconnecting to voice channel")
        if self._expecting_disconnect or self.voice_socket is None:
            raise DisconnectedFromVoiceError("Unable to send paket: disconnected from voice channel")

        self._audio_encoder.sent_to(
            voice_socket=self.voice_socket,  # type: ignore
            ip=self.voice_server_ip,  # type: ignore
            port=self.voice_server_port,  # type: ignore
            secret_key=self.secret_key,  # type: ignore
            ssrc=self.ssrc,  # type: ignore
            mode=self.voice_encryption_mode,  # type: ignore
            data=data,
            encode=encode
        )
