import unittest
from unittest.mock import patch, MagicMock, call
from indexer.rabbitmq_manager import RabbitMQManager, RabbitMQConfig
import json
import pika

class TestRabbitMQManager(unittest.TestCase):
    def setUp(self) -> None:
        self.config = RabbitMQConfig(
            host="test-host",
            port=1234,
            queue_name="test-queue"
        )
        self.manager = RabbitMQManager(self.config)

    @patch('pika.BlockingConnection')
    def test_get_connection_creates_new_connection(self, mock_conn: MagicMock) -> None:
        mock_connection = MagicMock()
        mock_channel = MagicMock()
        mock_conn.return_value = mock_connection
        mock_connection.channel.return_value = mock_channel

        connection, channel = self.manager._get_connection()

        self.assertEqual(connection, mock_connection)
        self.assertEqual(channel, mock_channel)
        
        # Verify BlockingConnection was called with correct parameters
        args = mock_conn.call_args[0][0]
        self.assertEqual(args.host, self.config.host)
        self.assertEqual(args.port, self.config.port)
        self.assertEqual(args.retry_delay, self.config.retry_delay)
        self.assertEqual(args.connection_attempts, self.config.connection_attempts)
        self.assertEqual(args.heartbeat, self.config.heartbeat)

        # Verify queue_declare was called
        mock_channel.queue_declare.assert_called_once_with(
            queue=self.config.queue_name,
            durable=True
        )

    @patch('pika.BlockingConnection')
    def test_get_connection_reuses_existing_connection(self, mock_conn: MagicMock) -> None:
        mock_connection = MagicMock()
        mock_channel = MagicMock()
        mock_conn.return_value = mock_connection
        mock_connection.channel.return_value = mock_channel
        
        # First call creates the connection
        self.manager._get_connection()
        
        # Simulate connection/channel being alive
        mock_connection.is_closed = False
        mock_channel.is_closed = False

        # Second call should reuse without creating a new one
        self.manager._get_connection()

        # Verify BlockingConnection was called only once
        mock_conn.assert_called_once()

    @patch('pika.BlockingConnection')
    def test_get_connection_reconnects_after_close(self, mock_conn: MagicMock) -> None:
        mock_connection1 = MagicMock()
        mock_channel1 = MagicMock()
        mock_connection2 = MagicMock()
        mock_channel2 = MagicMock()

        # Setup first connection
        mock_conn.side_effect = [mock_connection1, mock_connection2]
        
        mock_connection1.channel.return_value = mock_channel1
        mock_connection2.channel.return_value = mock_channel2

        # Initial call creates connection1
        self.manager._get_connection()
        
        # Close the connection
        mock_connection1.is_closed = True
        
        # Second call should create connection2
        self.manager._get_connection()

        # Verify BlockingConnection was called twice
        self.assertEqual(mock_conn.call_count, 2)

        # Verify queue_declare was called on both channels
        mock_channel1.queue_declare.assert_called_once()
        mock_channel2.queue_declare.assert_called_once()

    @patch('json.loads')
    def test_process_consumption_callback(self, mock_loads: MagicMock) -> None:
        # Setup
        mock_callback = MagicMock()
        self.config.consumption_callback = mock_callback
        
        mock_ch = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = "test-tag"
        mock_properties = MagicMock()
        mock_body = b'{"key": "value"}'
        
        mock_message = {"key": "value"}
        mock_loads.return_value = mock_message
        
        # Execute
        self.manager._process_consumption_callback(mock_ch, mock_method, mock_properties, mock_body)
        
        # Verify
        mock_loads.assert_called_once_with(mock_body)
        mock_callback.assert_called_once_with(mock_message)
        mock_ch.basic_ack.assert_called_once_with(delivery_tag=mock_method.delivery_tag)

    @patch('pika.BlockingConnection')
    def test_start_consuming(self, mock_conn: MagicMock) -> None:
        # Setup
        mock_connection = MagicMock()
        mock_channel = MagicMock()
        mock_conn.return_value = mock_connection
        mock_connection.channel.return_value = mock_channel
        
        # Execute
        with patch.object(self.manager, '_get_connection', return_value=(mock_connection, mock_channel)):
            # We don't actually want to block in the test
            # Instead of raising an exception, just make start_consuming return normally
            mock_channel.start_consuming.return_value = None
            
            self.manager.start_consuming()
        
        # Verify
        mock_channel.basic_qos.assert_called_once_with(prefetch_count=1)
        mock_channel.basic_consume.assert_called_once_with(
            queue=self.config.queue_name, 
            on_message_callback=self.manager._process_consumption_callback
        )
        mock_channel.start_consuming.assert_called_once()

    @patch('json.dumps')
    @patch('pika.BasicProperties')
    def test_publish_message(self, mock_properties: MagicMock, mock_dumps: MagicMock) -> None:
        # Setup
        mock_connection = MagicMock()
        mock_channel = MagicMock()
        
        # Patch _get_connection to return our mocks
        with patch.object(self.manager, '_get_connection', return_value=(mock_connection, mock_channel)):
            test_item = {"file_path": "test/path.txt"}
            mock_dumps.return_value = '{"file_path": "test/path.txt"}'
            mock_basic_props = MagicMock()
            mock_properties.return_value = mock_basic_props
            
            # Execute
            self.manager.publish_message(test_item)
        
        # Verify
        mock_dumps.assert_called_once_with(test_item)
        mock_properties.assert_called_once_with(delivery_mode=2)
        mock_channel.basic_publish.assert_called_once_with(
            exchange="",
            routing_key=self.config.queue_name,
            body=mock_dumps.return_value,
            properties=mock_basic_props
        )

if __name__ == '__main__':
    unittest.main()
