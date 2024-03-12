from src.core.exceptions import BaseBotError


class VoiceConnectionError(BaseBotError):
    pass


class DisconnectedFromVoiceError(VoiceConnectionError):
    pass


class TemporaryDisconnectedFromVoiceError(VoiceConnectionError):
    pass
