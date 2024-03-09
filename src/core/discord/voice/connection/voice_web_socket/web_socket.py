import logging
from typing import Dict, Union, Any

from discord import SpeakingState
from discord.gateway import VoiceKeepAliveHandler
from discord.types.voice import SupportedModes

from src.core.discord.voice.connection.base.voice_web_socket import BaseCustomVoiceWebSocket

_logger = logging.getLogger(f"voice_web_socket.{__name__}")


class CustomVoiceWebSocket(BaseCustomVoiceWebSocket):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self._ssrc_map: Dict[str, Dict[str, Any]] = {}

    @property
    def ssrc_map(self) -> Dict[str, Dict[str, Any]]:
        return self._ssrc_map

    async def send_resume(self) -> None:
        await self.send_as_json({
            "op": self.RESUME,
            "d": {
                "token": self.state.token,
                "server_id": str(self.state.server_id),
                "session_id": self.state.session_id,
            }
        })

    async def send_identify(self) -> None:
        await self.send_as_json({
            "op": self.IDENTIFY,
            "d": {
                "server_id": str(self.state.server_id),
                "user_id": str(self.state.user.id),
                "session_id": self.state.session_id,
                "token": self.state.token,
            },
        })

    async def send_select_protocol(self, ip: str, port: int, mode: 'SupportedModes') -> None:
        await self.send_as_json({
            "op": self.SELECT_PROTOCOL,
            "d": {
                "protocol": "udp",
                "data": {
                    "address": ip,
                    "port": port,
                    "mode": mode
                },
            }
        })

    async def send_speak(self, state: Union[SpeakingState, bool] = SpeakingState.voice) -> None:
        await self.send_as_json({
            "op": self.SPEAKING,
            "d": {
                "speaking": int(state),
                "delay": 0
            }
        })

    async def _load_secret_key(self, data: Dict[str, Any]) -> None:
        _logger.info("Received secret key for voice connection")

        self._secret_key = self.state.secret_key = data.get("secret_key", None)
        await self.send_speak(False)

    async def _init_connection(self, data: Dict[str, Any]) -> None:
        self.state.ssrc = data["ssrc"]
        self.state.port = data["port"]
        self.state.ip = data["ip"]

        modes = [mode for mode in data["modes"] if mode in self.state.supported_modes]
        mode = modes[0]

        await self.send_select_protocol(self.state.ip, self.state.port, mode)
        _logger.info(f"Selected the voice protocol for use ({mode})")

    async def process_received_message(self, data: Dict[str, Any]) -> None:
        _logger.debug(f"Voice websocket frame received: {data}")

        opcode = data["op"]

        if opcode == self.READY:
            message_data = data["d"]
            await self._init_connection(message_data)

        elif opcode == self.HEARTBEAT_ACK:
            self._keep_alive.ack()  # type: ignore

        elif opcode == self.RESUMED:
            _logger.info("Voice RESUME succeeded.")

        elif opcode == self.SESSION_DESCRIPTION:
            message_data = data["d"]
            self._state.mode = message_data["mode"]
            await self._load_secret_key(message_data)

        elif opcode == self.HELLO:
            message_data = data["d"]
            interval = message_data["heartbeat_interval"] / 1000.0
            # This class implements the same interface, so we can use VoiceKeepAliveHandler
            self._keep_alive = VoiceKeepAliveHandler(ws=self, interval=min(interval, 5.0))  # type: ignore
            self._keep_alive.start()

        elif opcode == self.SPEAKING:
            message_data = data["d"]

            ssrc = message_data["ssrc"]
            user = int(message_data["user_id"])
            speaking = message_data["speaking"]

            if ssrc in self._ssrc_map:
                self._ssrc_map[ssrc]["speaking"] = speaking
            else:
                self._ssrc_map.update({ssrc: {"user_id": user, "speaking": speaking}})

        if self._hook is not None and (message_data := data["d"]) is not None:
            await self._hook(self, opcode, message_data)
