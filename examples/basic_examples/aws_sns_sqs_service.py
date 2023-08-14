from typing import Any

import tomodachi
from tomodachi import Options, aws_sns_sqs, aws_sns_sqs_publish
from tomodachi.envelope import JsonBase


class ExampleAWSSNSSQSService(tomodachi.Service):
    name = "example-aws-sns-sqs-service"

    # The message envelope class defines how a message should be processed when sent and received
    # See tomodachi/envelope/json_base.py for a basic example using JSON and transferring some metadata
    message_envelope = JsonBase

    # Some options can be specified to define credentials, used ports, hostnames, access log, etc.
    options = Options(
        aws_sns_sqs=Options.AWSSNSSQS(
            region_name=None,  # Specify AWS region (example: "eu-west-1")
            aws_access_key_id=None,  # Specify AWS access key (example: "AKIA****************"")
            aws_secret_access_key=None,  # Specify AWS secret key (example: "****************************************")
        ),
    )

    @aws_sns_sqs("example-route1", queue_name="queue-1")
    async def route1a(self, data: Any) -> None:
        tomodachi.get_logger().info('Received data (function: route1a) - "{}"'.format(data))

    @aws_sns_sqs("example-route1", queue_name="queue-2")
    async def route1b(self, data: Any) -> None:
        tomodachi.get_logger().info('Received data (function: route1b) - "{}"'.format(data))

    @aws_sns_sqs("example-route2", queue_name="queue-3")
    async def route2(self, data: Any) -> None:
        tomodachi.get_logger().info('Received data (function: route2) - "{}"'.format(data))

    async def _started_service(self) -> None:
        async def publish(data: Any, topic: str) -> None:
            tomodachi.get_logger().info('Publish data "{}"'.format(data))
            await aws_sns_sqs_publish(self, data, topic=topic, wait=False)

        await publish("友達", "example-route1")
        await publish("other data", "example-route2")
