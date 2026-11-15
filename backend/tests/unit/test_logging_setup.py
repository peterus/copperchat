import logging
from collections.abc import Iterator

import pytest

from copperchat.__main__ import configure_logging


@pytest.fixture
def clean_logging() -> Iterator[logging.Logger]:
    """Saves and restores the logging state; tests clear the root handlers themselves
    because pytest adds its capture handlers only after fixture setup."""
    root = logging.getLogger()
    app = logging.getLogger("copperchat")
    saved = (root.handlers[:], root.level, app.level)
    yield app
    root.handlers, root.level = saved[0], saved[1]
    app.setLevel(saved[2])


def _bare_root(app: logging.Logger) -> logging.Logger:
    root = logging.getLogger()
    root.handlers = []
    root.setLevel(logging.WARNING)
    app.setLevel(logging.NOTSET)
    return root


def test_app_info_logs_get_a_handler(clean_logging: logging.Logger) -> None:
    # uvicorn configures only its own loggers: without this, copperchat INFO lines (push
    # sent/suppressed) never reached `docker logs`.
    root = _bare_root(clean_logging)
    configure_logging()
    assert [type(h) for h in root.handlers] == [logging.StreamHandler]
    assert root.level == logging.WARNING  # third-party libraries stay quiet
    assert logging.getLogger("copperchat.push.notifier").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)


def test_existing_configuration_is_respected(clean_logging: logging.Logger) -> None:
    root = _bare_root(clean_logging)
    handler = logging.NullHandler()
    root.addHandler(handler)
    clean_logging.setLevel(logging.ERROR)
    configure_logging()
    assert root.handlers == [handler]
    assert clean_logging.level == logging.ERROR
