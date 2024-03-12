import logging
from typing import Dict, Union, Any, List

from discord import SpeakingState
from discord.gateway import VoiceKeepAliveHandler
from discord.types.voice import SupportedModes

from src.core.discord.voice.connection.base.voice_web_socket import BaseCustomVoiceWebSocket

_logger = logging.getLogger(f"custom_voice.{__name__}")


class CustomVoiceWebSocket(BaseCustomVoiceWebSocket):
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

        self._state.secret_key = data.get("secret_key", None)
        self._is_ready = True

        await self.send_speak(False)

    def _select_mode(self, modes: List[str]) -> 'SupportedModes':
        available_modes = [mode for mode in modes if mode in self.state.supported_modes]
        return available_modes[0]  # type: ignore

    async def _init_connection(self, data: Dict[str, Any]) -> None:
        self._state.ssrc = data["ssrc"]

        ip = data["ip"]
        port = data["port"]
        mode = self._select_mode(data["modes"])

        self.state.set_voice_server_protocol(ip, port)
        await self.send_select_protocol(ip, port, mode)

        _logger.info(f"Selected the voice protocol ({mode})")

    async def process_received_message(self, data: Dict[str, Any]) -> None:
        _logger.debug(f"Voice websocket frame received: {data}")

        opcode = data["op"]

        if opcode == self.READY:
            message_data = data["d"]
            await self._init_connection(message_data)

        elif opcode == self.HEARTBEAT_ACK:
            self._keep_alive_handler.ack()  # type: ignore

        elif opcode == self.RESUMED:
            _logger.info("Voice RESUME succeeded.")

        elif opcode == self.SESSION_DESCRIPTION:
            message_data = data["d"]
            self._state.voice_encryption_mode = message_data["mode"]
            await self._load_secret_key(message_data)

        elif opcode == self.HELLO:
            message_data = data["d"]
            interval = message_data["heartbeat_interval"] / 1000.0
            # This class implements the same interface, so we can use VoiceKeepAliveHandler
            self._keep_alive_handler = VoiceKeepAliveHandler(ws=self, interval=min(interval, 5.0))  # type: ignore
            self._keep_alive_handler.start()

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
