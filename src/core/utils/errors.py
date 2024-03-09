from traceback import format_exception


def get_traceback_text(error: BaseException) -> str:
    """
    Returns traceback text

    :param error: `BaseException`
        Exception

    :return: `str`
        Traceback text
    """

    return "".join(format_exception(type(error), error, error.__traceback__))
