import logging
import struct
from abc import ABC
from typing import List, Any, Optional, Callable, TypeAlias, TYPE_CHECKING

import discord
import nacl.secret  # type: ignore
import nacl.utils  # type: ignore
from discord import MISSING
from discord import opus
from discord.player import AudioSource
from discord.types.voice import SupportedModes

from src.core.discord.voice.audio_player import CustomAudioPlayer
from src.core.discord.voice.connection.base import BaseCustomVoiceClient

if TYPE_CHECKING:
    from discord.opus import Encoder

_logger = logging.getLogger(__name__)

# Type aliases
PlayAfterCallback: TypeAlias = Callable[[Optional[Exception]], Any]


class CustomVoiceClientAudioMixin(BaseCustomVoiceClient, ABC):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.sequence: int = 0
        self.timestamp: int = 0
        self._lite_nonce: int = 0

        self._player: Optional[CustomAudioPlayer] = None
        self.encoder: Encoder = MISSING

    @property
    def latency(self) -> float:
        return self.connection.ws.latency if self.connection.ws else float("inf")

    @property
    def average_latency(self) -> float:
        return self.connection.ws.average_latency if self.connection.ws else float("inf")

    @property
    def secret_key(self) -> List[int]:
        return self.connection.secret_key

    @property
    def ssrc(self) -> int:
        return self.connection.ssrc

    @property
    def mode(self) -> SupportedModes:  # FIXME: SupportedModes | None returned
        return self.connection.mode  # type: ignore

    @property
    def source(self) -> Optional[AudioSource]:
        return self._player.source if self._player else None

    @source.setter
    def source(self, value: AudioSource) -> None:
        if not isinstance(value, AudioSource):
            raise TypeError(f'expected AudioSource not {value.__class__.__name__}.')

        if self._player is None:
            raise ValueError('Not playing anything.')

        self._player.set_source(value)

    @staticmethod
    def strip_header_ext(data: bytes) -> bytes:
        if data[0] == 0xBE and data[1] == 0xDE and len(data) > 4:
            _, length = struct.unpack_from(">HH", data)
            offset = 4 + length * 4
            data = data[offset:]

        return data

    def is_playing(self) -> bool:
        return self._player is not None and self._player.is_playing()

    def is_paused(self) -> bool:
        return self._player is not None and self._player.is_paused()

    def checked_add(self, attr: str, value: int, limit: int) -> None:
        val = getattr(self, attr)

        if val + value > limit:
            setattr(self, attr, 0)
        else:
            setattr(self, attr, val + value)

    def _encrypt_xsalsa20_poly1305(self, header: bytes, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))
        nonce = bytearray(24)
        nonce[:12] = header

        return header + box.encrypt(bytes(data), bytes(nonce)).ciphertext

    def _encrypt_xsalsa20_poly1305_suffix(self, header: bytes, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))
        nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)

        return header + box.encrypt(bytes(data), nonce).ciphertext + nonce

    def _encrypt_xsalsa20_poly1305_lite(self, header: bytes, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))
        nonce = bytearray(24)

        nonce[:4] = struct.pack('>I', self._lite_nonce)
        self.checked_add('_lite_nonce', 1, 4294967295)

        return header + box.encrypt(bytes(data), bytes(nonce)).ciphertext + nonce[:4]

    def _decrypt_xsalsa20_poly1305(self, header, data):
        box = nacl.secret.SecretBox(bytes(self.secret_key))

        nonce = bytearray(24)
        nonce[:12] = header

        return self.strip_header_ext(box.decrypt(bytes(data), bytes(nonce)))

    def _decrypt_xsalsa20_poly1305_suffix(self, header, data):
        box = nacl.secret.SecretBox(bytes(self.secret_key))

        nonce_size = nacl.secret.SecretBox.NONCE_SIZE
        nonce = data[-nonce_size:]

        return self.strip_header_ext(box.decrypt(bytes(data[:-nonce_size]), nonce))

    def _decrypt_xsalsa20_poly1305_lite(self, header, data):
        box = nacl.secret.SecretBox(bytes(self.secret_key))

        nonce = bytearray(24)
        nonce[:4] = data[-4:]
        data = data[:-4]

        return self.strip_header_ext(box.decrypt(bytes(data), bytes(nonce)))

    def _get_voice_packet(self, data):
        header = bytearray(12)

        # Formulate rtp header
        header[0] = 0x80
        header[1] = 0x78
        struct.pack_into('>H', header, 2, self.sequence)
        struct.pack_into('>I', header, 4, self.timestamp)
        struct.pack_into('>I', header, 8, self.ssrc)

        encrypt_packet = getattr(self, '_encrypt_' + self.mode)
        return encrypt_packet(header, data)

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        self.checked_add('sequence', 1, 65535)

        if encode and not self.encoder:
            self.encoder = opus.Encoder(application=2049)  # 'audio'

        encoded_data = self.encoder.encode(data, self.encoder.SAMPLES_PER_FRAME) if encode else data
        packet = self._get_voice_packet(encoded_data)

        try:
            self.connection.send_packet(packet)
        except OSError:
            _logger.info('A packet has been dropped (seq: %s, timestamp: %s)', self.sequence, self.timestamp)

        self.checked_add('timestamp', opus.Encoder.SAMPLES_PER_FRAME, 4294967295)

    def play(self, source: discord.AudioSource, *, after: Optional[PlayAfterCallback] = None) -> None:
        if not self.is_connected():
            raise discord.ClientException('Not connected to voice.')

        if self.is_playing():
            raise discord.ClientException('Already playing audio.')

        if not isinstance(source, discord.AudioSource):
            raise TypeError(f'source must be an AudioSource not {source.__class__.__name__}')

        if not source.is_opus():
            self.encoder = opus.Encoder()

        self._player = CustomAudioPlayer(source, self, after=after)
        self._player.start()

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
