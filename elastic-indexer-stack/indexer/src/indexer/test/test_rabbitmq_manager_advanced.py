import unittest
from unittest.mock import patch, MagicMock, call
import time
import queue
import pika
import pika.exceptions
import logging
from indexer.rabbitmq_manager import RabbitMQManager, RabbitMQConfig

class TestRabbitMQManagerAdvanced(unittest.TestCase):
    
    def setUp(self):
        self.config = RabbitMQConfig(
            host="test-host", 
            port=1234,
            queue_name="test-queue"
        )
        self.logger = MagicMock(spec=logging.Logger)
        self.manager = RabbitMQManager(self.config, logger=self.logger)
    
    def test_worker_loop_handles_amqp_connection_error_with_backoff(self):
        # Setup
        self.manager._channel = MagicMock()
        self.manager._connection = MagicMock()
        
        # Create mock queue that raises AMQPConnectionError
        mock_queue = MagicMock()
        conn_error = pika.exceptions.AMQPConnectionError("Connection error")
        mock_queue.get.side_effect = conn_error
        
        original_queue = self.manager._worker_queue
        self.manager._worker_queue = mock_queue
        
        try:
            # Patch time.sleep to avoid actual delay and stop after a few calls
            with patch('time.sleep') as mock_sleep:
                call_count = 0
                
                def sleep_side_effect(seconds):
                    nonlocal call_count
                    call_count += 1
                    if call_count >= 2:
                        self.manager._should_stop = True
                
                mock_sleep.side_effect = sleep_side_effect
                
                self.manager._worker_loop()
                
                # Verify exponential backoff behavior
                self.assertEqual(mock_sleep.call_count, 2)
                mock_sleep.assert_has_calls([call(1.0)])
                
        finally:
            # Restore the original queue
            self.manager._worker_queue = original_queue
    
    def test_try_reconnect_successful(self):
        # Setup
        self.manager._connection = MagicMock()
        self.manager._connection.is_closed = True
        self.manager._channel = MagicMock()
        
        # Mock _connect method
        self.manager._connect = MagicMock()
        
        # Execute
        self.manager._try_reconnect()
        
        # Verify
        self.manager._connect.assert_called_once()
        self.logger.info.assert_called_with("Reconnected to RabbitMQ")
    
    def test_try_reconnect_skips_when_stopping(self):
        # Setup
        self.manager._should_stop = True
        self.manager._connect = MagicMock()
        
        # Execute
        self.manager._try_reconnect()
        
        # Verify
        self.manager._connect.assert_not_called()
    
    def test_try_reconnect_handles_exception(self):
        # Setup
        self.manager._connection = MagicMock()
        self.manager._connection.is_closed = True
        error = Exception("Connection error")
        self.manager._connect = MagicMock(side_effect=error)
        
        # Execute
        self.manager._try_reconnect()
        
        # Verify
        self.manager._connect.assert_called_once()
        self.logger.error.assert_called_with(f"Failed to reconnect: {error}")
    
    def test_close_handles_stop_consuming_exception(self):
        # Setup
        self.manager._channel = MagicMock()
        self.manager._channel.is_closed = False
        self.manager._connection = MagicMock()
        self.manager._connection.is_closed = False
        error = Exception("Stop consuming error")
        self.manager._channel.stop_consuming.side_effect = error
        
        # Execute
        self.manager.close()
        
        # Verify
        self.manager._channel.stop_consuming.assert_called_once()
        self.logger.warning.assert_called_with(f"Error stopping consumption: {error}")
        self.manager._connection.close.assert_called_once()
    
    def test_close_handles_worker_thread_join_exception(self):
        # Setup
        self.manager._connection = MagicMock()
        self.manager._connection.is_closed = False
        self.manager._worker_thread = MagicMock()
        self.manager._worker_thread.is_alive.return_value = True
        error = Exception("Thread join error")
        self.manager._worker_thread.join.side_effect = error
        
        # Execute
        with patch('time.sleep'):  # Mock sleep to avoid delay
            self.manager.close()
        
        # Verify
        self.manager._worker_thread.join.assert_called_once_with(timeout=2.0)
        self.logger.warning.assert_called_with(f"Error joining worker thread: {error}")
        self.manager._connection.close.assert_called_once()
    
    def test_close_handles_connection_close_exception(self):
        # Setup
        self.manager._connection = MagicMock()
        self.manager._connection.is_closed = False
        error = Exception("Connection close error")
        self.manager._connection.close.side_effect = error
        
        # Execute
        self.manager.close()
        
        # Verify
        self.manager._connection.close.assert_called_once()
        self.logger.error.assert_called_with(f"Error closing RabbitMQ connection: {error}")
    
    def test_close_handles_no_active_connection(self):
        # Setup - no connection set
        self.manager._connection = None
        
        # Execute
        self.manager.close()
        
        # Verify - should not raise any exceptions
        self.assertTrue(self.manager._should_stop)
    
    def test_worker_loop_requeues_unprocessed_message_when_stopping(self):
        # Setup
        test_message = {"file_path": "test/path.txt"}
        test_tag = "delivery-tag-123"
        
        # Create mock queue
        mock_queue = MagicMock()
        original_queue = self.manager._worker_queue
        self.manager._worker_queue = mock_queue
        
        try:
            # Configure mock queue behavior - first return message, then raise Empty
            call_count = [0]
            
            def get_side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    self.manager._should_stop = True  # Set stop flag on first call
                    return (test_message, test_tag)
                raise queue.Empty()  # Subsequent calls raise Empty to exit loop
                
            mock_queue.get.side_effect = get_side_effect
            
            # Execute worker loop
            self.manager._worker_loop()
            
            # Verify - message was put back in queue
            mock_queue.put.assert_called_once_with((test_message, test_tag))
            
        finally:
            # Restore the original queue
            self.manager._worker_queue = original_queue

    def test_try_reconnect_handles_connection_close_errors(self):
        """Test that errors during connection close are properly ignored during reconnection."""
        # Setup
        mock_connection = MagicMock()
        mock_connection.is_closed = False
        self.manager._connection = mock_connection
        self.manager._channel = MagicMock()
        
        # Make connection.close raise an exception
        close_error = Exception("Error closing connection")
        mock_connection.close.side_effect = close_error
        
        # Mock _connect to avoid actual network operations
        self.manager._connect = MagicMock()
        
        # Execute
        self.manager._try_reconnect()
        
        # Verify
        # 1. close was called despite the error
        mock_connection.close.assert_called_once()
        # 2. connection and channel were reset
        self.assertIsNone(self.manager._connection)
        self.assertIsNone(self.manager._channel)
        # 3. reconnection was still attempted
        self.manager._connect.assert_called_once()
        # 4. success was logged
        self.logger.info.assert_called_with("Reconnected to RabbitMQ")
    
    def test_try_reconnect_with_already_closed_connection(self):
        """Test reconnection when the connection is already closed."""
        # Setup
        mock_connection = MagicMock()
        mock_connection.is_closed = True  # Connection is already closed
        self.manager._connection = mock_connection
        self.manager._channel = MagicMock()
        
        # Mock _connect to avoid actual network operations
        self.manager._connect = MagicMock()
        
        # Execute
        self.manager._try_reconnect()
        
        # Verify
        # 1. close was not called on an already closed connection
        mock_connection.close.assert_not_called()
        # 2. connection and channel were reset
        self.assertIsNone(self.manager._connection)
        self.assertIsNone(self.manager._channel)
        # 3. reconnection was still attempted
        self.manager._connect.assert_called_once()
        self.manager._connect.assert_called_once()

    def test_worker_loop_handles_amqp_connection_error_with_exponential_backoff(self):
        """Test that AMQP connection errors trigger exponential backoff after multiple failures."""
        # Setup
        self.manager._channel = MagicMock()
        self.manager._connection = MagicMock()
        mock_queue = MagicMock()
        
        # Store original queue and replace with mock
        original_queue = self.manager._worker_queue
        self.manager._worker_queue = mock_queue
        
        # Configure mock queue to raise AMQPConnectionError
        conn_error = pika.exceptions.AMQPConnectionError("Connection error")
        mock_queue.get.side_effect = conn_error
        
        try:
            # Mock time.sleep to avoid actual delays and track call arguments
            sleep_calls = []
            
            def mock_sleep_side_effect(seconds):
                sleep_calls.append(seconds)
                # After we've collected enough calls, stop the loop
                if len(sleep_calls) >= 6:
                    self.manager._should_stop = True
            
            with patch('time.sleep', side_effect=mock_sleep_side_effect) as mock_sleep:
                # Execute
                self.manager._worker_loop()
                
                # Verify exponential backoff behavior
                self.assertEqual(mock_sleep.call_count, 6)
                # First 4 calls should be 1 second (below max failures threshold)
                self.assertEqual(sleep_calls[:4], [1.0, 1.0, 1.0, 1.0])
                # 5th call should be exponential backoff (failures = 5)
                self.assertEqual(sleep_calls[4], 10.0)  # min(30, 5*2)
                # 6th call should be higher (failures = 6)
                self.assertEqual(sleep_calls[5], 12.0)  # min(30, 6*2)
                
                # Verify error log for backoff
                self.logger.error.assert_any_call("Too many connection failures (5), backing off...")
                
        finally:
            # Restore the original queue
            self.manager._worker_queue = original_queue

    def test_process_consumption_callback_handles_json_decode_error(self):
        """Test that invalid JSON messages are acknowledged and not processed."""
        # Setup
        mock_channel = MagicMock()
        mock_method = MagicMock()
        mock_method.delivery_tag = "tag123"
        mock_properties = MagicMock()
        invalid_json_body = b'This is not valid JSON'
        
        # Execute
        self.manager._process_consumption_callback(
            mock_channel, mock_method, mock_properties, invalid_json_body
        )
        
        # Verify behavior
        self.logger.error.assert_called_once()
        self.assertIn("Failed to parse message body", self.logger.error.call_args[0][0])
        mock_channel.basic_ack.assert_called_once_with(delivery_tag="tag123")
        
        # Verify the message wasn't put in the worker queue
        self.assertEqual(self.manager._worker_queue.qsize(), 0)