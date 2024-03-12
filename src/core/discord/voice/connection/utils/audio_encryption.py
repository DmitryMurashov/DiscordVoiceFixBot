import socket
import struct

import nacl.secret
import nacl.utils

from typing import SupportsBytes, List
from discord.opus import Encoder as DiscordOpusEncoder
from discord.types.voice import SupportedModes
import logging

_logger = logging.getLogger(f"custom_voice.{__name__}")


class AudioEncoder:
    def __init__(self) -> None:
        self._opus_encoder: DiscordOpusEncoder = DiscordOpusEncoder(application=2049)  # 'audio'
        self._lite_nonce: int = 0
        self._sequence: int = 0
        self._timestamp: int = 0

    def _limited_add(self, attr: str, value: int, limit: int) -> None:
        current_value = getattr(self, attr)

        if current_value + value > limit:
            setattr(self, attr, 0)
        else:
            setattr(self, attr, current_value + value)

    def _encrypt_xsalsa20_poly1305(self, header: bytes, data: SupportsBytes, secret_key: List[int]) -> bytes:
        return encrypt_xsalsa20_poly1305(secret_key, header, data)

    def _encrypt_xsalsa20_poly1305_suffix(self, header: bytes, data: SupportsBytes, secret_key: List[int]) -> bytes:
        return encrypt_xsalsa20_poly1305_suffix(secret_key, header, data)

    def _encrypt_xsalsa20_poly1305_lite(self, header: bytes, data: SupportsBytes, secret_key: List[int]) -> bytes:
        result = encrypt_xsalsa20_poly1305_lite(secret_key, header, data, self._lite_nonce)
        self._limited_add('_lite_nonce', 1, 4294967295)
        return result

    def create_voice_packet(self, data: bytes, ssrc: int, mode: 'SupportedModes', secret_key: List[int]) -> bytes:
        header = bytearray(12)

        # Formulate rtp header
        header[0] = 0x80
        header[1] = 0x78
        struct.pack_into('>H', header, 2, self._sequence)
        struct.pack_into('>I', header, 4, self._timestamp)
        struct.pack_into('>I', header, 8, ssrc)

        encrypt_function = getattr(self, '_encrypt_' + mode)  # type: ignore
        return encrypt_function(header, data, secret_key)

    def sent_to(self,
                voice_socket: socket.socket,
                ip: str,
                port: int,
                secret_key: List[int],
                ssrc: int,
                mode: 'SupportedModes',
                data: bytes,
                encode: bool = True) -> None:
        self._limited_add('_sequence', 1, 65535)

        encoded_data = self._opus_encoder.encode(data, self._opus_encoder.SAMPLES_PER_FRAME) if encode else data
        packet = self.create_voice_packet(encoded_data, ssrc, mode, secret_key)

        try:
            voice_socket.sendto(packet, (ip, port))
        except OSError as error:
            _logger.info(f'A packet has been dropped (seq: {self._sequence}, timestamp: {self._timestamp}): {error}')

        self._limited_add('_timestamp', DiscordOpusEncoder.SAMPLES_PER_FRAME, 4294967295)


def strip_header_ext(data: bytes) -> bytes:
    if data[0] == 0xBE and data[1] == 0xDE and len(data) > 4:
        _, length = struct.unpack_from(">HH", data)
        offset = 4 + length * 4
        data = data[offset:]

    return data


def encrypt_xsalsa20_poly1305(secret_key: List[int], header: bytes, data: SupportsBytes) -> bytes:
    box = nacl.secret.SecretBox(bytes(secret_key))
    nonce = bytearray(24)
    nonce[:12] = header

    return header + box.encrypt(bytes(data), bytes(nonce)).ciphertext


def encrypt_xsalsa20_poly1305_suffix(secret_key: List[int], header: bytes, data: SupportsBytes) -> bytes:
    box = nacl.secret.SecretBox(bytes(secret_key))
    nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)

    return header + box.encrypt(bytes(data), nonce).ciphertext + nonce


def encrypt_xsalsa20_poly1305_lite(secret_key: List[int], header: bytes, data: SupportsBytes, lite_nonce: int) -> bytes:
    box = nacl.secret.SecretBox(bytes(secret_key))
    nonce = bytearray(24)
    nonce[:4] = struct.pack('>I', lite_nonce)

    return header + box.encrypt(bytes(data), bytes(nonce)).ciphertext + nonce[:4]


def decrypt_xsalsa20_poly1305(secret_key: List[int], header: bytes, data: SupportsBytes) -> bytes:
    box = nacl.secret.SecretBox(bytes(secret_key))

    nonce = bytearray(24)
    nonce[:12] = header

    return strip_header_ext(box.decrypt(bytes(data), bytes(nonce)))


def decrypt_xsalsa20_poly1305_suffix(secret_key: List[int], _: bytes, data: SupportsBytes) -> bytes:
    box = nacl.secret.SecretBox(bytes(secret_key))

    nonce_size = nacl.secret.SecretBox.NONCE_SIZE
    nonce = data[-nonce_size:]

    return strip_header_ext(box.decrypt(bytes(data[:-nonce_size]), nonce))


def decrypt_xsalsa20_poly1305_lite(secret_key: List[int], _: bytes, data: SupportsBytes) -> bytes:
    box = nacl.secret.SecretBox(bytes(secret_key))

    nonce = bytearray(24)
    nonce[:4] = data[-4:]
    data = data[:-4]

    return strip_header_ext(box.decrypt(bytes(data), bytes(nonce)))
