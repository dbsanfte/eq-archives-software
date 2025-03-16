import os
import logging
from dataclasses import dataclass
from typing import Callable
import pika
import pika.channel
import json

@dataclass
class RabbitMQConfig:
    host: str = os.environ.get("RABBITMQ_HOST", "rabbitmq")
    port: int = os.environ.get("RABBITMQ_PORT", 5672)
    queue_name: str = os.environ.get("RABBITMQ_QUEUE", "file_paths")
    retry_delay: float = os.environ.get("RABBITMQ_RETRY_DELAY", 5.0)
    connection_attempts: int = os.environ.get("RABBITMQ_CONNECTION_ATTEMPTS", 25)
    heartbeat: int = os.environ.get("RABBITMQ_HEARTBEAT", 60)
    consumption_callback: Callable[[dict], None] = lambda _: None

class RabbitMQManager:
    def __init__(self, config: RabbitMQConfig, logger: logging.Logger=None):
        self._logger = logger or logging.getLogger(__name__)
        self._config = config
        self._connection = None
        self._channel = None

    def _connect(self) -> None:
        if (self._connection is not None 
            and not self._connection.is_closed 
            and self._channel is not None 
            and not self._channel.is_closed):
            return  # Already connected
        
        self._logger.debug("Initiating new connection to RabbitMQ...")
        params = pika.ConnectionParameters(
            host=self._config.host,
            port=self._config.port,
            retry_delay=self._config.retry_delay,
            connection_attempts=self._config.connection_attempts,
            heartbeat=self._config.heartbeat
        )
        self._connection = pika.BlockingConnection(params)
        self._logger.debug("RabbitMQ connection established.")
        self._channel = self._connection.channel()
        self._logger.debug(f"Declaring queue '{self._config.queue_name}'...")
        self._channel.queue_declare(queue=self._config.queue_name, durable=True)
        self._logger.debug("RabbitMQ channel and queue established.")
        
    def _get_connection(self) -> tuple[pika.BlockingConnection, pika.channel.Channel]:
        self._connect()
        return self._connection, self._channel
    
    def _process_consumption_callback(self, ch, method, properties, body):
        message = json.loads(body)
        self._config.consumption_callback(message)
        ch.basic_ack(delivery_tag=method.delivery_tag)
    
    def start_consuming(self):
        self._connect()
        self._channel.basic_qos(prefetch_count=1)
        self._channel.basic_consume(queue=self._config.queue_name, on_message_callback=self._process_consumption_callback)
        self._logger.info(" [*] Waiting for messages. Press CTRL-C to exit.")
        self._channel.start_consuming()
        
    def publish_message(self, item: dict) -> None:
        _, channel = self._get_connection()
        channel.basic_publish(
            exchange="",
            routing_key=self._config.queue_name,
            body=json.dumps(item),
            properties=pika.BasicProperties(delivery_mode=2)
        )
        self._logger.debug(f"Published item to queue: {item}")
