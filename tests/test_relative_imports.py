from typing import Any

import tomodachi
from run_test_service_helper import start_service


def test_relative_import_service(capsys: Any, loop: Any) -> None:
    services, future = start_service("tests/services/relative_service.py", loop=loop)

    assert services is not None
    assert len(services) == 1
    instance = services.get("test_relative")
    assert instance is not None
    assert instance.start is True
    assert instance.started is True
    assert instance.stop is False

    async def _async_kill():
        tomodachi.exit()

    loop.create_task(_async_kill())
    loop.run_until_complete(future)

    assert instance.stop is True


def test_relative_import_service_without_py_ending(capsys: Any, loop: Any) -> None:
    services, future = start_service("tests/services/relative_service", loop=loop)

    instance = services.get("test_relative")
    assert instance is not None

    async def _async_kill():
        tomodachi.exit()

    loop.create_task(_async_kill())
    loop.run_until_complete(future)
