import os
import logging
from dataclasses import dataclass
from typing import Callable, Optional
import pika
from pika.adapters.blocking_connection import BlockingChannel
import pika.exceptions
import json
import threading
import queue
import time


@dataclass
class RabbitMQConfig:
    """Configuration for RabbitMQ connections and operations."""
    host: str = os.environ.get("RABBITMQ_HOST", "rabbitmq")
    port: int = int(os.environ.get("RABBITMQ_PORT", 5672))
    queue_name: str = os.environ.get("RABBITMQ_QUEUE", "file_paths")
    retry_delay: float = float(os.environ.get("RABBITMQ_RETRY_DELAY", 5.0))
    connection_attempts: int = int(os.environ.get("RABBITMQ_CONNECTION_ATTEMPTS", 25))
    heartbeat: int = int(os.environ.get("RABBITMQ_HEARTBEAT", 60))
    consumption_callback: Callable[[dict], None] = lambda _: None


class RabbitMQManager:
    """
    Manages RabbitMQ connections, message publishing and consumption.
    Provides thread-safe message processing with error handling and reconnection logic.
    """
    
    def __init__(self, config: RabbitMQConfig, logger: Optional[logging.Logger] = None):
        """
        Initialize the RabbitMQ manager.
        
        Args:
            config: Configuration for RabbitMQ connections
            logger: Optional logger instance
        """
        self._logger = logger or logging.getLogger(__name__)
        self._config = config
        self._connection = None
        self._channel = None
        self._worker_queue = queue.Queue()
        self._should_stop = False
        self._worker_thread = None

    def _connect(self) -> None:
        """Establish a connection to RabbitMQ if not already connected."""
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
        
    def _get_connection(self) -> tuple[pika.BlockingConnection, BlockingChannel]:
        """
        Get the current connection and channel, connecting if necessary.
        
        Returns:
            Tuple of (connection, channel)
        
        Raises:
            AssertionError: If connection or channel could not be established
        """
        self._connect()
        # After connecting, these should never be None, so assert their proper types
        assert self._connection is not None, "Failed to establish RabbitMQ connection"
        assert self._channel is not None, "Failed to create RabbitMQ channel"
        return self._connection, self._channel
    
    def _process_consumption_callback(self, ch, method, properties, body):
        """
        Process a single message received from RabbitMQ.
        Instead of processing synchronously, we place it in a thread-safe queue
        for the worker thread to consume.
        
        Args:
            ch: Channel object
            method: Contains delivery tag and other info
            properties: Message properties
            body: Message body
        """
        # Parse the message body
        try:
            message_data = json.loads(body)
            # Place the message in the worker queue along with its delivery tag
            self._worker_queue.put((message_data, method.delivery_tag))
            # Process any connection heartbeats to keep connection alive
            if self._connection is not None:
                self._connection.process_data_events(time_limit=0)
        except json.JSONDecodeError:
            # If we can't parse the message, log an error and acknowledge it
            # to remove it from the queue
            self._logger.error(f"Failed to parse message body: {body!r}")
            ch.basic_ack(delivery_tag=method.delivery_tag)
    
    def _worker_loop(self):
        """
        Worker loop that processes messages from the queue.
        Runs in a separate thread to handle message processing.
        """
        failures = 0
        max_failures = 5  # Maximum number of connection failures before backing off
        
        while not self._should_stop:
            try:
                # Get a message from the queue with a timeout
                message, delivery_tag = self._worker_queue.get(timeout=1)
                
                # Check if we should stop before processing
                if self._should_stop:
                    # Put the message back in the queue for next time
                    self._worker_queue.put((message, delivery_tag))
                    break
                
                # Process the single message    
                self._process_single_message(message, delivery_tag)
                    
            except queue.Empty:
                # No messages in the queue, just continue
                pass
            except (pika.exceptions.AMQPConnectionError, 
                    pika.exceptions.ChannelError) as conn_err:
                failures += 1
                if failures >= max_failures:
                    self._logger.error(
                        f"Too many connection failures ({failures}), backing off..."
                    )
                    time.sleep(min(30, failures * 2))  # Exponential backoff
                else:
                    time.sleep(1)  # Short delay before retry
            except Exception as e:
                self._logger.error(f"Error in worker loop: {e}")
                time.sleep(0.5)  # Avoid tight loops on persistent errors
    
    def _process_single_message(self, message, delivery_tag):
        """
        Process a single message from the queue.
        This method is extracted from the worker loop for better testability.
        
        Args:
            message: The message content to process
            delivery_tag: The delivery tag for acknowledgment
        """
        try:
            # Process the message using the callback
            self._config.consumption_callback(message)
            
            # Use thread-safe way to acknowledge the message in the main thread
            if not self._should_stop and self._connection and not self._connection.is_closed:
                try:
                    # Create a closure that captures the current value of delivery_tag
                    def ack_function(tag=delivery_tag):
                        self._safe_ack(tag)
                        
                    self._connection.add_callback_threadsafe(ack_function)
                except Exception as err:
                    self._logger.warning(f"Error scheduling ACK: {err}")
                    if not self._should_stop:
                        self._worker_queue.put((message, delivery_tag))
            else:
                self._logger.warning(
                    "Cannot acknowledge message - connection closed or stopping"
                )
                # Don't requeue if we're stopping
                if not self._should_stop:
                    self._worker_queue.put((message, delivery_tag))
                
        except Exception as e:
            # If processing fails, negative-acknowledge the message
            self._logger.error(f"Error processing message: {e}")
            
            # Only NACK if connection is still valid and we're not stopping
            if not self._should_stop and self._connection and not self._connection.is_closed:
                try:
                    # Use thread-safe way to NACK in the main thread with proper closure
                    def nack_function(tag=delivery_tag):
                        self._safe_nack(tag)
                        
                    self._connection.add_callback_threadsafe(nack_function)
                except Exception as err:
                    self._logger.warning(f"Error scheduling NACK: {err}")
                    if not self._should_stop:
                        self._worker_queue.put((message, delivery_tag))
            else:
                self._logger.warning(
                    "Cannot NACK message - connection or channel closed or stopping"
                )
                if not self._should_stop:
                    self._worker_queue.put((message, delivery_tag))

    def _safe_ack(self, delivery_tag):
        """Safely acknowledge a message from the main thread."""
        try:
            if self._channel and not self._channel.is_closed:
                self._channel.basic_ack(delivery_tag=delivery_tag)
                self._logger.debug(f"Successfully ACKed message {delivery_tag}")
        except (pika.exceptions.ConnectionClosed, 
                pika.exceptions.ChannelClosed, 
                pika.exceptions.AMQPError) as conn_err:
            self._logger.warning(f"Connection error during safe ACK: {conn_err}")
            self._try_reconnect()
        except Exception as e:
            self._logger.error(f"Error in _safe_ack: {e}")

    def _safe_nack(self, delivery_tag):
        """Safely negative-acknowledge a message from the main thread."""
        try:
            if self._channel and not self._channel.is_closed:
                self._channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
                self._logger.debug(f"Successfully NACKed message {delivery_tag}")
        except (pika.exceptions.ConnectionClosed, 
                pika.exceptions.ChannelClosed, 
                pika.exceptions.AMQPError) as conn_err:
            self._logger.warning(f"Connection error during safe NACK: {conn_err}")
            self._try_reconnect()
        except Exception as e:
            self._logger.error(f"Error in _safe_nack: {e}")

    def _try_reconnect(self):
        """Attempt to reconnect after connection issues."""
        try:
            if self._should_stop:
                return  # Don't reconnect if we're shutting down
                
            self._logger.info("Attempting to reconnect to RabbitMQ...")
            if self._connection and not self._connection.is_closed:
                try:
                    self._connection.close()
                except:
                    pass  # Ignore errors on close
                    
            # Reset connection and channel
            self._connection = None
            self._channel = None
            
            # Try to reconnect
            self._connect()
            self._logger.info("Reconnected to RabbitMQ")
        except Exception as e:
            self._logger.error(f"Failed to reconnect: {e}")

    def start_consuming(self):
        """
        Starts consuming messages from the queue.
        Initiates a worker thread that processes messages.
        """
        self._should_stop = False  # Reset the flag at the beginning
        # Use _get_connection to ensure valid connection and channel
        _, self._channel = self._get_connection()
        
        # Start worker thread if not already running
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._should_stop = False
            self._worker_thread = threading.Thread(target=self._worker_loop)
            self._worker_thread.daemon = True
            self._worker_thread.start()
        
        try:
            self._channel.basic_qos(prefetch_count=1)
            self._channel.basic_consume(
                queue=self._config.queue_name, 
                on_message_callback=self._process_consumption_callback
            )
            self._logger.info(" [*] Waiting for messages. Press CTRL-C to exit.")
            
            # Start consuming but occasionally process data events to maintain heartbeat
            self._channel.start_consuming()
        except KeyboardInterrupt:
            self._logger.info("Stopping consuming...")
            self._should_stop = True  # Signal worker thread to stop
            try:
                # Give worker thread a moment to process the stop signal
                time.sleep(0.5)
                self._channel.stop_consuming()
                # Wait for worker to finish current task
                if self._worker_thread and self._worker_thread.is_alive():
                    self._worker_thread.join(timeout=5.0)
            except Exception as e:
                self._logger.error(f"Error during shutdown after KeyboardInterrupt: {e}")
        except Exception as e:
            self._logger.error(f"Error in start_consuming: {e}")
            self._logger.exception(e)
            self._should_stop = True  # Signal worker thread to stop
            # Give worker thread a moment to process the stop signal
            time.sleep(0.5)
            # Attempt to close the connection gracefully
            try:
                self._channel.stop_consuming()
            except Exception as close_error:
                self._logger.error(f"Error stopping consumption: {close_error}")

    def publish_message(self, item: dict) -> None:
        """
        Publish a message to the RabbitMQ queue.
        
        Args:
            item: Dictionary representing the message to publish
        """
        _, channel = self._get_connection()
        channel.basic_publish(
            exchange="",
            routing_key=self._config.queue_name,
            body=json.dumps(item),
            properties=pika.BasicProperties(delivery_mode=2)
        )
        self._logger.debug(f"Published item to queue: {item}")

    def close(self):
        """Close the RabbitMQ connection and stop consuming."""
        self._logger.debug("Closing RabbitMQ connection...")
        self._should_stop = True
        
        # Stop consuming first if needed
        if self._channel and not self._channel.is_closed:
            try:
                self._channel.stop_consuming()
            except Exception as e:
                self._logger.warning(f"Error stopping consumption: {e}")
        
        # Give worker thread a chance to see the stop flag
        time.sleep(0.5)
        
        if self._worker_thread and self._worker_thread.is_alive():
            try:
                self._worker_thread.join(timeout=2.0)
            except Exception as e:
                self._logger.warning(f"Error joining worker thread: {e}")
        
        if self._connection and not self._connection.is_closed:
            try:
                self._connection.close()
                self._logger.debug("RabbitMQ connection closed successfully")
            except Exception as e:
                self._logger.error(f"Error closing RabbitMQ connection: {e}")
