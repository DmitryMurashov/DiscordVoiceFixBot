import logging

from discord.player import AudioPlayer, AudioSource

_logger = logging.getLogger(__name__)


class CustomAudioPlayer(AudioPlayer):
    """
    Custom extension of the AudioPlayer class with additional functionality.
    """

    def set_source(self, source: AudioSource) -> None:
        self._set_source(source)
