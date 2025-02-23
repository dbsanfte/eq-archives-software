# new file
import logging
import pika
import os

logger = logging.getLogger(__name__)

def connect_rabbitmq():
    logger.debug("Initiating new connection to RabbitMQ...")
    connection_params = pika.ConnectionParameters(
        host=os.environ.get("RABBITMQ_HOST", "rabbitmq"),
        port=os.environ.get("RABBITMQ_PORT", "5672"),
        retry_delay=5,
        connection_attempts=25,
        heartbeat=60
    )
    connection = pika.BlockingConnection(connection_params)
    logger.debug("RabbitMQ connection established.")
    channel = connection.channel()
    channel.queue_declare(queue=os.environ.get("RABBITMQ_QUEUE", "file_paths"), durable=True)
    logger.debug("RabbitMQ channel and queue established.")
    return connection, channel

def get_rabbitmq_connection():
    if (not hasattr(get_rabbitmq_connection, "connection") 
            or get_rabbitmq_connection.connection.is_closed 
            or get_rabbitmq_connection.channel.is_closed):        
        logger.debug("Creating new RabbitMQ connection...")
        get_rabbitmq_connection.connection, get_rabbitmq_connection.channel = connect_rabbitmq()
    return get_rabbitmq_connection.connection, get_rabbitmq_connection.channel