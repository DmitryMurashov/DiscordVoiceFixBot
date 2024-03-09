from .base import VoiceConnectionError


class DisconnectedFromVoiceError(VoiceConnectionError):
    pass


class TemporaryDisconnectedFromVoiceError(VoiceConnectionError):
    pass
