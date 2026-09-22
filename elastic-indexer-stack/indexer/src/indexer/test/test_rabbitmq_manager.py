import unittest
import time
from unittest.mock import patch, MagicMock, call
from indexer.rabbitmq_manager import RabbitMQManager, RabbitMQConfig
import json
import pika
import queue

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

    @patch('pika.BlockingConnection')
    def test_start_consuming_creates_worker_thread(self, mock_conn: MagicMock) -> None:
        # Setup
        mock_connection = MagicMock()
        mock_channel = MagicMock()
        mock_conn.return_value = mock_connection
        mock_connection.channel.return_value = mock_channel
        
        # Create a more specific patch for threading.Thread
        with patch('threading.Thread') as mock_thread:
            mock_thread_instance = MagicMock()
            mock_thread.return_value = mock_thread_instance
            
            # Also patch _connect to avoid actual network but ensure both connection and channel are set
            def side_effect():
                self.manager._connection = mock_connection
                self.manager._channel = mock_channel
            with patch.object(self.manager, '_connect', side_effect=side_effect):
                self.manager.start_consuming()
        
        # Verify thread was created with correct parameters
        mock_thread.assert_called_once_with(target=self.manager._worker_loop)
        self.assertEqual(mock_thread_instance.daemon, True)  # Check that daemon property was set
        mock_thread_instance.start.assert_called_once()

    def test_start_consuming_handles_keyboard_interrupt(self):
        # Setup
        mock_connection = MagicMock()
        mock_channel = MagicMock()
        mock_thread = MagicMock()
        self.manager._worker_thread = mock_thread
        
        # Configure start_consuming to raise KeyboardInterrupt
        mock_channel.start_consuming.side_effect = KeyboardInterrupt()
        
        # Execute
        with patch.object(self.manager, '_connect'), \
             patch.object(self.manager, '_channel', mock_channel), \
             patch.object(self.manager, '_connection', mock_connection):
            self.manager.start_consuming()
        
        # Verify proper shutdown sequence
        self.assertTrue(self.manager._should_stop)
        mock_channel.stop_consuming.assert_called_once()
        # Verify thread handling
        mock_thread.join.assert_called_once_with(timeout=5.0)

    def test_start_consuming_handles_general_exception(self):
        # Setup
        mock_connection = MagicMock()
        mock_channel = MagicMock()
        test_error = Exception("Test exception")
        mock_channel.start_consuming.side_effect = test_error
        
        # Execute
        with patch.object(self.manager, '_connect'), \
             patch.object(self.manager, '_channel', mock_channel), \
             patch.object(self.manager, '_connection', mock_connection), \
             patch.object(self.manager._logger, 'error') as mock_error_log, \
             patch.object(self.manager._logger, 'exception') as mock_exception_log:
            self.manager.start_consuming()
        
        # Verify exception handling
        self.assertTrue(self.manager._should_stop)
        mock_channel.stop_consuming.assert_called_once()
        # Verify logging
        mock_error_log.assert_called_once()
        mock_exception_log.assert_called_once_with(test_error)

    def test_start_consuming_handles_stop_consuming_exception(self):
        # Setup
        mock_connection = MagicMock()
        mock_channel = MagicMock()
        
        # First raise an exception in start_consuming, then in stop_consuming
        mock_channel.start_consuming.side_effect = Exception("Start consuming failed")
        mock_channel.stop_consuming.side_effect = Exception("Stop consuming failed")
        
        # Execute
        with patch.object(self.manager, '_connect'), \
             patch.object(self.manager, '_channel', mock_channel), \
             patch.object(self.manager, '_connection', mock_connection), \
             patch.object(self.manager._logger, 'error') as mock_error_log:
            self.manager.start_consuming()
        
        # Verify exception in stop_consuming doesn't propagate
        self.assertTrue(self.manager._should_stop)
        mock_channel.stop_consuming.assert_called_once()
        # Should have at least one error log (may have more from nested exceptions)
        self.assertGreaterEqual(mock_error_log.call_count, 1)

    def test_start_consuming_creates_new_worker_thread_when_needed(self):
        # Setup - simulate no existing thread
        self.manager._worker_thread = None
        mock_connection = MagicMock()
        mock_channel = MagicMock()
        
        # Execute
        with patch.object(self.manager, '_connect'), \
             patch.object(self.manager, '_channel', mock_channel), \
             patch.object(self.manager, '_connection', mock_connection), \
             patch('threading.Thread') as mock_thread:
            mock_thread_instance = MagicMock()
            mock_thread.return_value = mock_thread_instance
            
            # Return immediately without blocking
            mock_channel.start_consuming.side_effect = Exception("Test early exit")
            
            self.manager.start_consuming()
        
        # Verify thread creation and configuration
        mock_thread.assert_called_once_with(target=self.manager._worker_loop)
        self.assertTrue(mock_thread_instance.daemon)  # Check daemon property
        mock_thread_instance.start.assert_called_once()

    def test_process_consumption_callback(self) -> None:
        # Setup
        mock_channel = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = "tag123"
        mock_properties = MagicMock()
        mock_body = b'{"file_path": "test/path.txt"}'
        
        # Patch the connection to check if process_data_events is called
        self.manager._connection = MagicMock()
        
        # Execute
        self.manager._process_consumption_callback(mock_channel, mock_method, mock_properties, mock_body)
        
        # Verify message was placed in the queue and heartbeat was processed
        self.assertEqual(self.manager._worker_queue.qsize(), 1)
        message, tag = self.manager._worker_queue.get()
        self.assertEqual(message, {"file_path": "test/path.txt"})
        self.assertEqual(tag, "tag123")
        self.manager._connection.process_data_events.assert_called_once_with(time_limit=0)

    @patch('time.sleep')
    def test_worker_loop_handles_empty_queue(self, mock_sleep: MagicMock) -> None:
        # Setup - directly control the stop flag
        self.manager._should_stop = False
        
        # More direct approach - make queue.get raise Empty once then stop the loop
        call_count = [0]
        
        # Store reference to manager for the closure
        manager_ref = self.manager
        
        def mock_get(self_queue, timeout=None):
            call_count[0] += 1
            # After first call, set the stop flag
            if call_count[0] >= 1:
                manager_ref._should_stop = True
            raise queue.Empty()
        
        # Execute with patching
        with patch('queue.Queue.get', mock_get):
            self.manager._worker_loop()
        
        # Verify the loop stopped
        self.assertTrue(self.manager._should_stop)

    def test_close_stops_worker_thread(self) -> None:
        # Setup - more complete mocking
        self.manager._connection = MagicMock()
        # Need to set is_closed to False for the condition check in close()
        self.manager._connection.is_closed = False
        self.manager._should_stop = False
        
        # Execute 
        self.manager.close()
        
        # Verify only the necessary checks
        self.assertTrue(self.manager._should_stop)
        self.manager._connection.close.assert_called_once()

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

    def test_start_consuming_configures_channel_correctly(self):
        # Setup
        mock_connection = MagicMock()
        mock_channel = MagicMock()
        
        # Exit early with exception after setup
        mock_channel.start_consuming.side_effect = Exception("Test early exit")
        
        # Execute
        with patch.object(self.manager, '_connect'), \
             patch.object(self.manager, '_channel', mock_channel), \
             patch.object(self.manager, '_connection', mock_connection), \
             patch.object(self.manager._logger, 'error'):
            self.manager.start_consuming()
        
        # Verify channel setup
        mock_channel.basic_qos.assert_called_once_with(prefetch_count=1)
        mock_channel.basic_consume.assert_called_once_with(
            queue=self.manager._config.queue_name,
            on_message_callback=self.manager._process_consumption_callback
        )

if __name__ == '__main__':
    unittest.main()
