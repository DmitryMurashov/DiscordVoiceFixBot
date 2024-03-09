from typing import Optional, ClassVar


class BaseBotError(Exception):
    """
    Base bot exception
    """

    default_message: ClassVar[str] = 'Unknown error occurred'

    def __init__(self, message: Optional[str] = None) -> None:
        """
        Initialize BotError instance.

        :param message: `Optional[str]`
            (Optional) Error message.
            If not specified will be changed to default_message that defined in class
        """

        if message is None:
            message = self.__class__.default_message

        self.message = message

        super().__init__(message)
