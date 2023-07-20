from typing import Any

from run_test_service_helper import start_service


def test_empty_service(capsys: Any, loop: Any) -> None:
    services, future = start_service("tests/services/empty_service.py", loop=loop)

    loop.run_until_complete(future)

    out, err = capsys.readouterr()
    assert "no transport handlers defined" in (out + err)


def test_non_decorated_service(capsys: Any, loop: Any) -> None:
    services, future = start_service("tests/services/non_decorated_service.py", loop=loop)

    loop.run_until_complete(future)

    out, err = capsys.readouterr()
    assert "no transport handlers defined" in (out + err)
