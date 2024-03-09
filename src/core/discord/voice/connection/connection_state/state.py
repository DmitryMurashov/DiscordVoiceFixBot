import asyncio
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

from src.core.discord.voice.connection.base import BaseCustomVoiceConnectionState, WebsocketHook
from src.core.discord.voice.connection.exceptions import DisconnectedFromVoiceError, TemporaryDisconnectedFromVoiceError
from src.core.discord.voice.connection.voice_web_socket import CustomVoiceWebSocket
from src.core.utils.async_tools import AsyncEventProvider, SyncAndAsyncEvent
from src.core.utils.errors import get_traceback_text
from .socker_reader import SocketReader

_logger = logging.getLogger(__name__)


class CustomVoiceConnectionState(BaseCustomVoiceConnectionState):
    _VOICE_STATE_UPDATED_EVENT: str = "voice_state_updated"
    _VOICE_SERVER_UPDATED_EVENT: str = "voice_server_updated"
    _CONNECTED_EVENT: str = "connected"
    _DISCONNECTED_EVENT: str = "disconnected"

    _CONNECTION_ATTEMPTS: int = 5

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        # State
        self._event_provider: AsyncEventProvider = AsyncEventProvider()
        self._connected: SyncAndAsyncEvent = SyncAndAsyncEvent()
        self._expecting_disconnect: bool = False
        self._handshaking = False
        self._reconnecting = False

        # Tasks
        self._connector_task: Optional[asyncio.Task] = None
        self._dispatcher_task: Optional[asyncio.Task] = None

        # Socker reader
        self._socket_reader = SocketReader(self)
        self._socket_reader.start()

    @property
    def _is_connecting(self) -> bool:
        return self._connector_task is not None and not self._connector_task.done()

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _update_voice_channel(self, channel_id: Optional[int]) -> None:
        self.channel = channel_id and self.guild.get_channel(channel_id)  # type: ignore

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

        if self._handshaking:
            self._event_provider.dispatch_event(self._VOICE_STATE_UPDATED_EVENT)

    async def _on_voice_server_update(self, data: 'VoiceServerUpdatePayload') -> None:
        self.token = data.get("token")
        self.server_id = int(data["guild_id"])

        endpoint = data.get("endpoint")
        if endpoint is None or self.token is None:
            _logger.warning(
                "Awaiting endpoint... This requires waiting. "
                "If timeout occurred considering raising the timeout and reconnecting."
            )
            return

        self.endpoint, _, _ = endpoint.rpartition(":")
        if self.endpoint.startswith("wss://"):
            # Just in case, strip it off since we're going to add it later
            self.endpoint = self.endpoint[6:]

        # This gets set later
        await self._create_voice_socket()

        if self._handshaking:
            self._event_provider.dispatch_event(self._VOICE_SERVER_UPDATED_EVENT)
        else:
            _logger.debug("Closing websocket with code 4000")

            if self.ws is not None and not self.ws.is_closed:
                await self.ws.close(code=4000)

    async def on_voice_state_update(self, data: 'GuildVoiceStatePayload') -> None:
        _logger.debug("Got voice state update")

        await self._on_voice_state_update(data)

    async def on_voice_server_update(self, data: 'VoiceServerUpdatePayload') -> None:
        _logger.debug("Got voice server update")

        await self._on_voice_server_update(data)

    async def _create_voice_socket(self) -> None:
        _logger.debug("Creating voice socket")

        if self.socket is not None:
            self.socket.close()

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        self._socket_reader.resume()

    async def _create_and_connect_websocket(self, *, resume_session: bool = False, hook: Optional[WebsocketHook] = None) -> CustomVoiceWebSocket:
        _logger.debug("Creating websocket")

        gateway = f'wss://{self.endpoint}/?v=4'
        client = self._voice_client
        http = client._state.http

        _logger.debug("Connecting websocket")

        sock = await http.ws_connect(gateway, compress=15)
        ws = CustomVoiceWebSocket(
            state=self,
            web_socket=sock,
            loop=client.loop,
            hook=hook
        )

        ws._max_heartbeat_timeout = 60.0  # type: ignore
        ws.thread_id = threading.get_ident()  # type: ignore

        if resume_session:
            await ws.send_resume_session()
        else:
            await ws.send_identify()

        return ws

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

    async def _close_connection_safe(self, cleanup: bool = False) -> None:
        _logger.info('Closing connection')

        # Stop the websocket reader because closing the websocket will trigger an unwanted reconnect
        if self._dispatcher_task is not None:
            self._stop_dispatcher()

        try:
            if self.ws:
                await self.ws.close()
        except Exception as error:
            _logger.debug(f"Ignoring exception in close_connection_safe: {error.__class__.__name__}: {error}")
        finally:
            self.ip = None  # MISSING
            self.port = None  # MISSING
            self._socket_reader.pause()

            # Flip the connected event to unlock any waiters
            self._connected.set()
            self._connected.clear()

            if self.socket is not None:
                self.socket.close()

            if cleanup:
                self._socket_reader.stop()
                self._voice_client.cleanup()

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

    async def _handshake_websocket(self) -> None:
        _logger.debug("Handshaking websocket")

        async with asyncio.timeout(self.timeout):
            try:
                while not self.ws.is_ready:  # type: ignore
                    await self.ws.poll_event()  # type: ignore
            except discord.ConnectionClosed as error:
                if error.code == 4006:
                    _logger.debug("Session is no linger valid. Aborting connection")
                    raise asyncio.CancelledError("Session is no linger valid")

        _logger.debug("Websocket handshake successful")

    async def _handshake(self, self_deaf: bool, self_mute: bool) -> None:
        self._handshaking = True

        await self._send_voice_connect(self_deaf=self_deaf, self_mute=self_mute)

        _logger.debug("Waiting for updates")

        try:
            await self._event_provider.wait_for_events(
                (self._VOICE_STATE_UPDATED_EVENT, self._VOICE_SERVER_UPDATED_EVENT),
                timeout=self.timeout
            )
        except asyncio.TimeoutError as error:
            _logger.debug("Timeout waiting for updates. Closing connection")

            await self._close_connection_safe(cleanup=True)
            raise error
        finally:
            self._handshaking = False

        _logger.debug("Updates performed. Connecting...")

    async def _connector(self, *, reconnect: bool, self_deaf: bool, self_mute: bool, resume_session: bool) -> None:
        _logger.info("Connector task started")

        for attempt in range(self._CONNECTION_ATTEMPTS):
            _logger.debug(f"Attempting to connect to voice (attempt {attempt})")

            try:
                await self._handshake(self_deaf=self_deaf, self_mute=self_mute)

                self.ws = await self._create_and_connect_websocket(resume_session=resume_session)
                await self._handshake_websocket()
                break
            except (discord.ConnectionClosed, asyncio.TimeoutError):
                if not reconnect:
                    raise

                sleep_time = 1 + attempt * 2.0
                _logger.exception(f"Failed to connect to voice. Retrying in {sleep_time}...")
                await asyncio.sleep(sleep_time)

                await self._send_voice_disconnect()
                continue

        _logger.debug("Successfully connected to voice")

        self._connected.set()
        self._event_provider.dispatch_event(self._CONNECTED_EVENT)

        if self._dispatcher_task is None:
            _logger.info("Creating dispatcher task")
            self._dispatcher_task = self.loop.create_task(self._poll_voice_ws(reconnect))

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

    async def _connect(self, self_deaf: bool, self_mute: bool, resume_session: bool, wait: bool = True) -> None:
        _logger.info(f"Connecting to the voice channel {self.channel.id}")

        if self._connector_task is not None:
            self._stop_connector()

        self._connector_task = self.loop.create_task(
            self._wrap_connector(
                self._connector,
                reconnect=self.reconnect,
                self_deaf=self_deaf,
                self_mute=self_mute,
                resume_session=resume_session
            )
        )

        # Just to set self._connector_task = None after connecting is finished
        self._connector_task.add_done_callback(lambda _: self._stop_connector())

        if wait:
            _logger.debug("Waiting for connector task to end")
            await self._connector_task

    async def connect(self, *, reconnect: bool, timeout: float, self_deaf: bool, self_mute: bool, resume: bool, wait: bool = True) -> None:
        if self._dispatcher_task is not None:
            self._stop_dispatcher()

        self.timeout = timeout
        self.reconnect = reconnect

        await self._connect(self_deaf=self_deaf, self_mute=self_mute, resume_session=resume, wait=wait)

    async def _potential_reconnect(self) -> bool:
        _logger.debug("Potential reconnecting")

        self._connected.clear()
        self._reconnecting = True
        self._handshaking = True

        try:
            await self._event_provider.wait_for_event(self._VOICE_SERVER_UPDATED_EVENT, timeout=self.timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._reconnecting = False
            self._handshaking = False

        for x in range(self._CONNECTION_ATTEMPTS):
            try:
                self.ws = await self._create_and_connect_websocket(resume_session=False)
                await self._handshake_websocket()
            except (discord.ConnectionClosed, asyncio.TimeoutError, OSError) as error:
                _logger.debug(
                    f"Potential reconnection failed. Retrying... (error: {error.__class__.__name__}: {error})")
                continue
            else:
                self._connected.set()
                self._reconnecting = False
                _logger.debug("Potential reconnection successful")
                return True

        _logger.warning("Potential reconnection failed")
        self._reconnecting = False
        return False

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
                    elif error.code == 4014:
                        _logger.info('Disconnected from voice by force')

                        if await self._potential_reconnect():
                            continue
                    else:
                        _logger.debug(f"'Not handling close code {error.code} ({error.reason or 'no reason'})'")

                    if not self._expecting_disconnect:
                        await self.disconnect()
                    return

                if not reconnect:
                    await self.disconnect()
                    raise

                sleep_time = backoff.delay()
                _logger.exception("Disconnected from voice... Reconnecting in %.2fs.", sleep_time)

                self._connected.clear()
                await asyncio.sleep(sleep_time)
                await self._send_voice_disconnect()

                try:
                    voice_state = self.self_voice_state

                    await self._connect(
                        self_deaf=voice_state.self_deaf if voice_state is not None else False,
                        self_mute=voice_state.self_mute if voice_state is not None else False,
                        resume_session=False
                    )
                except asyncio.TimeoutError:
                    # at this point we've retried 5 times... let's continue the loop.
                    _logger.warning("Could not connect to voice... Retrying...")
                    continue

    async def disconnect(self, *, force: bool = True, cleanup: bool = True) -> None:
        if not force and not self.is_connected():
            return

        try:
            await self._send_voice_disconnect()
            await self._close_connection_safe(cleanup=cleanup)
        except Exception as error:
            _logger.debug(f"Ignoring error while disconnecting: {error.__class__.__name__}: {error}")

    async def move_to(self, channel: Optional[Snowflake], timeout: Optional[float] = 30.0) -> None:
        if channel is None:  # Move to None channel = Disconnect
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

    async def async_wait_until_connected(self, timeout: Optional[float] = 30) -> bool:
        return await self._connected.wait_async(timeout=timeout)

    def wait_until_connected(self, timeout: Optional[float] = 30.0) -> bool:
        return self._connected.wait(timeout=timeout)

    def send_packet(self, packet: bytes) -> None:
        if self._expecting_disconnect or self.socket is None:
            raise DisconnectedFromVoiceError("Unable to send paket: disconnected from voice channel")
        elif self._reconnecting:
            raise TemporaryDisconnectedFromVoiceError("Unable to send paket: reconnecting to voice channel")

        self.socket.sendto(packet, (self.ip, self.port))

        # self.socket.sendall(packet)  # Original (doesn't work)
