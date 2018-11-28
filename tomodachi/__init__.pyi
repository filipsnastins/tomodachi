from tomodachi.__version__ import __version__ as __version__, __version_info__ as __version_info__
from tomodachi.invoker import decorator
from tomodachi.transport.amqp import amqp as amqp, amqp_publish as amqp_publish
from tomodachi.transport.aws_sns_sqs import aws_sns_sqs as aws_sns_sqs, aws_sns_sqs_publish as aws_sns_sqs_publish
from tomodachi.transport.http import HttpException as HttpException, Response as HttpResponse, http as http, http_error as http_error, http_static as http_static, websocket as websocket
from tomodachi.transport.schedule import daily as daily, heartbeat as heartbeat, hourly as hourly, minutely as minutely, monthly as monthly, schedule as schedule
from typing import Any, Optional

CLASS_ATTRIBUTE: str = ...


def service(cls: Any) -> Any: ...


class Service:
    TOMODACHI_SERVICE_CLASS: bool = ...
    log: Any = ...
    log_setup: Any = ...


def set_instance(name: str, instance: Any) -> None: ...


def get_instance(name: Optional[str] = None) -> Any: ...
