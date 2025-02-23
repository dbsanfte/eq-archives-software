import unittest
from unittest.mock import patch, MagicMock
from indexer.rabbitmq_manager import RabbitMQManager, RabbitMQConfig

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

if __name__ == '__main__':
    unittest.main()
