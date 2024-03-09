from .base import BaseBotError


class NoneObjectError(BaseBotError):
    pass


class InitializationError(BaseBotError):
    pass


class CancelOperationError(BaseBotError):
    pass


class ConfigurationError(BaseBotError):
    pass


class NotFoundError(BaseBotError):
    pass


class AlreadyExistsError(BaseBotError):
    pass
